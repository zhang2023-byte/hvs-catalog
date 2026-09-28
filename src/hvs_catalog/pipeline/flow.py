"""共享提取流程适配器：按登记方法的步骤推进名单、数值与收尾。

复用 ``runtime.create_execution``/``resume_execution`` 的固定输入快照、
锁、事务、裁决与恢复机制（``DefaultFlowAdapter`` 基类），不自建状态
机。裁决开关独立于方法与模型：开启时按原 ``ReviewService`` 提交歧义
并等待专家；关闭时不请求也不等待专家——日常模式保留未决，评测/合成
纯 AI 组允许模型尝试并把结果记为 ``model_judgments``（不写成专家决
定）。名单到数值的传递按名单实际内容校验：数值不接受被排除或未决
的对象，后续步骤失败不抹去已保存名单。每次调用的请求、响应与用量
先归档再提交步骤结果，恢复时先复用已归档响应再考虑重新调用。
"""

from __future__ import annotations

import json
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..records.common import RecordRef, build_record, sha256_of
from ..records.patches import PatchApplicationError, apply_decision_patch
from ..records.questions import DecisionRecord, QuestionRecord
from ..records.roster import RosterRecord
from ..records.runs import Attempt, ModelJudgment, StepRecord, Usage
from ..review import ReviewService, derive_question_id
from ..runtime import (
    ApplicationPlan,
    DefaultFlowAdapter,
    RuntimeContext,
    append_run_revision,
)
from . import mock
from .params import (
    ParameterPolicy,
    ParameterPolicyError,
    effective_policy,
)
from .registry import MethodSpec

FLOW_ID = "hvs-extraction"
FLOW_VERSION = "1"

STEP_INTERPRET = "interpret_ambiguity"


class ExtractionState(TypedDict, total=False):
    execution_id: str
    method: str
    mode: str
    paper_key: str
    material: dict
    expert_adjudication: bool
    rules_version: str
    candidates: list[dict]
    roster_id: str
    roster_refs: list[dict]
    paper_id: str
    contribution_refs: list[dict]
    question_refs: list[dict]
    summary: dict


def _ref_dict(ref: RecordRef) -> dict:
    return ref.model_dump(mode="json")


def _ref(payload: dict) -> RecordRef:
    return RecordRef.model_validate(payload)


def _question_refs(refs: list[dict]) -> list[dict]:
    return [ref for ref in refs if ref["record_id"].startswith("q-")]


def _canonical_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


class ExtractionFlow(DefaultFlowAdapter):
    """按登记方法规格执行的提取流程；``fault_hooks`` 仅供测试注入。

    运行模式（daily/evaluation/synthetic）参与流程标识：同一请求键下
    换模式会得到不同的执行摘要，不会静默复用另一模式的执行；恢复
    时当前适配器与固定模式不符会被拒绝。
    """

    flow_id = FLOW_ID
    flow_version = FLOW_VERSION

    def __init__(
        self,
        spec: MethodSpec,
        *,
        mode: str,
        fault_hooks: dict[str, Any] | None = None,
        parameter_policy: ParameterPolicy | None = None,
    ) -> None:
        if mode not in ("daily", "evaluation", "synthetic"):
            raise ValueError(f"未知运行模式：{mode!r}")
        self.spec = spec
        self.mode = mode
        self.flow_id = f"{FLOW_ID}-{mode}"
        self.fault_hooks = fault_hooks or {}
        self.parameter_policy = parameter_policy

    def hook(self, name: str) -> None:
        callback = self.fault_hooks.get(name)
        if callback is not None:
            callback()

    # ---- 流程定义 ----

    def initial_state(self, ctx: RuntimeContext) -> dict:
        material = self._resolve_material(ctx)
        snapshot = ctx.execution.input_snapshot
        return {
            "execution_id": ctx.execution.execution_id,
            "method": self.spec.method_id,
            "mode": self.mode,
            "paper_key": material["paper_key"],
            "material": material,
            "expert_adjudication": snapshot.expert_adjudication,
            "rules_version": snapshot.rules.version,
            "candidates": [],
            "roster_id": "",
            "roster_refs": [],
            "paper_id": "",
            "contribution_refs": [],
            "question_refs": [],
            "summary": None,
        }

    def _resolve_material(self, ctx: RuntimeContext) -> dict:
        """按快照固定的完整 RecordRef 读取并核对登记模拟材料。

        来源编号后续升版（甚至内容换成另一论文）不影响已固定执行；
        准确修订缺失、或修订与可信登记的来源身份/受支持修订/文件指纹
        不一致时在此拒绝（与入口运行前检查共用 ``resolve_registered_``
        ``material``），不降级读 current，不改写旧快照，也不发出任何
        服务请求。
        """
        refs = ctx.execution.input_snapshot.source_refs
        if len(refs) != 1:
            raise ValueError(
                f"模拟实现目前只支持单一来源包，本执行固定了 {len(refs)} 个"
            )
        ref = refs[0]
        record = ctx.store.read_record(ref)
        if record is None:
            raise ValueError(
                f"执行固定的来源修订缺失：{ref.record_id} v{ref.revision}；"
                "恢复不能改用当前或其他版本的来源"
            )
        return mock.resolve_registered_material(record)

    def build_graph(self, ctx: RuntimeContext):
        # 流程层防御：无论经哪个入口进入，固定快照中的模型参数都先按
        # 同一受限策略核对；未声明策略与入口一样按空策略处理，任何
        # 非空参数都会在任何服务调用前被拒绝
        policy = effective_policy(
            self.parameter_policy
            if self.parameter_policy is not None
            else self.spec.parameter_policy
        )
        for item in ctx.execution.input_snapshot.models:
            try:
                policy.validate(
                    item.parameters,
                    context=f"执行 {ctx.execution.execution_id} 固定的模型参数",
                )
            except ParameterPolicyError as exc:
                raise ValueError(
                    f"执行固定的模型参数未通过受限策略校验，拒绝发出服务请求：{exc}"
                ) from exc
        builder = StateGraph(ExtractionState)
        previous = START
        for step in self.spec.steps:
            handler = self._node_for(step.step_id)

            def node(state: dict, _handler=handler, _ctx=ctx) -> dict:
                return _handler(_ctx, state)

            builder.add_node(step.step_id, node)
            builder.add_edge(previous, step.step_id)
            previous = step.step_id
        builder.add_edge(previous, END)
        return builder.compile(checkpointer=ctx.checkpointer)

    def _node_for(self, step_id: str):
        nodes = {
            "direct_roster": self._direct_roster,
            "discover_candidates": self._discover_candidates,
            "review_candidates": self._review_candidates,
            "quantities": self._quantities,
            "finalize": self._finalize,
        }
        try:
            return nodes[step_id]
        except KeyError:  # pragma: no cover - 登记规格已限定步骤边界
            raise ValueError(f"未知步骤：{step_id}") from None

    # ---- 共用输入与服务调用 ----

    def _model_spec(self, ctx: RuntimeContext, step_id: str) -> dict:
        chosen = None
        default = None
        for item in ctx.execution.input_snapshot.models:
            if item.step_id is None:
                default = item
            elif item.step_id == step_id:
                chosen = item
        picked = chosen or default
        if picked is None:  # pragma: no cover - 入口已校验唯一默认模型
            raise ValueError(f"步骤 {step_id} 没有可用模型")
        return {
            "model": picked.model,
            "provider": picked.provider,
            "parameters": picked.parameters,
        }

    def _op_input(self, ctx: RuntimeContext, step_id: str, state: dict) -> dict:
        return {
            "execution_id": ctx.execution.execution_id,
            "step": step_id,
            "paper_key": state["paper_key"],
            "material_sha256": sha256_of({"spec": state["material"]}),
            "rules": state["rules_version"],
            "model": self._model_spec(ctx, step_id),
        }

    def _call(
        self,
        ctx: RuntimeContext,
        *,
        step_id: str,
        request: dict,
        state: dict,
        call_key: str,
    ) -> dict:
        """调用模拟服务并把请求/响应与用量归档到运行清单。

        服务步骤取自 ``request["step"]``；归档归属的 ``step_id`` 是方法
        步骤（解释尝试等服务子调用归档在所属方法步骤下，不产生额外的
        未完成步骤）。
        """
        started = ctx.now()
        service_step = str(request.get("step", step_id))
        response = ctx.service.call(
            step=service_step, request=request, material=state["material"]
        )
        self.hook(f"after_response:{step_id}")
        request_ref = ctx.space.publish_run_file(
            ctx.execution.execution_id,
            f"requests/{call_key}.json",
            _canonical_bytes(request),
        )
        response_ref = ctx.space.publish_run_file(
            ctx.execution.execution_id,
            f"responses/{call_key}.json",
            _canonical_bytes(response),
        )
        tokens = response.get("tokens")
        previous = self._step_attempts(ctx, step_id)
        append_run_revision(
            ctx.space,
            ctx.execution,
            step=StepRecord(
                step_id=step_id,
                state="running",
                input_refs=[],
                attempts=[
                    *previous,
                    Attempt(
                        request=request_ref,
                        response=response_ref,
                        state="succeeded",
                        started_at=started,
                        finished_at=ctx.now(),
                        usage=Usage(tokens=tokens, cost=None, currency=None),
                        cost=None,
                        currency=None,
                        service_request_id=None,
                    ),
                ],
                result_refs=self._step_result_refs(ctx, step_id),
                error_refs=[],
            ),
        )
        self.hook(f"after_archive:{step_id}")
        return response

    def _step_attempts(self, ctx: RuntimeContext, step_id: str) -> list:
        """读取步骤已归档的尝试，避免多次调用覆盖早先归档。"""
        run = ctx.space.store().read_record_current(ctx.execution.run_ref.record_id)
        step = (
            next((s for s in run.data.steps if s.step_id == step_id), None)
            if run is not None
            else None
        )
        return list(step.attempts) if step is not None else []

    def _step_result_refs(self, ctx: RuntimeContext, step_id: str) -> list:
        run = ctx.space.store().read_record_current(ctx.execution.run_ref.record_id)
        step = (
            next((s for s in run.data.steps if s.step_id == step_id), None)
            if run is not None
            else None
        )
        return list(step.result_refs) if step is not None else []

    def _stored_response(
        self, ctx: RuntimeContext, step_id: str, request: dict
    ) -> dict | None:
        """查找本步骤已归档的相同请求响应；命中则不再调用服务。"""
        run = ctx.space.store().read_record_current(ctx.execution.run_ref.record_id)
        step = (
            next((s for s in run.data.steps if s.step_id == step_id), None)
            if run is not None
            else None
        )
        if step is None:
            return None
        for attempt in reversed(step.attempts):
            if attempt.request is None or attempt.response is None:
                continue
            try:
                archived = json.loads(ctx.space.read_file(attempt.request))
            except (ValueError, OSError):  # pragma: no cover - 归档损坏按缺失处理
                continue
            if archived == request:
                return json.loads(ctx.space.read_file(attempt.response))
        return None

    def _call_or_reuse(
        self,
        ctx: RuntimeContext,
        *,
        step_id: str,
        request: dict,
        state: dict,
        call_key: str,
    ) -> dict:
        stored = self._stored_response(ctx, step_id, request)
        if stored is not None:
            return stored
        return self._call(
            ctx, step_id=step_id, request=request, state=state, call_key=call_key
        )

    def _await_decisions(
        self, ctx: RuntimeContext, question_refs: list[dict], step_id: str
    ) -> None:
        """开启专家裁决时等待已保存问题被应用；未决保持暂停。"""
        review = ReviewService(ctx.space)
        for ref_dict in question_refs:
            while True:
                view = review.get_question(_ref(ref_dict))
                if view.application is not None and view.application.phase == "applied":
                    break
                interrupt(
                    {
                        "question_ref": ref_dict,
                        "step_id": step_id,
                        "prompt": view.record.data.prompt,
                    }
                )

    def _pure_ai_allowed(self) -> bool:
        """纯 AI 解释例外仅限评测/合成的关闭专家组。"""
        return self.mode in ("evaluation", "synthetic")

    def _attempt_interpretation(
        self,
        ctx: RuntimeContext,
        *,
        state: dict,
        step_id: str,
        question_ref: RecordRef,
    ) -> None:
        """纯 AI 组的解释尝试：结果记为模型判断，不写成专家决定。

        模拟材料固定返回 unresolved：问题保持未决、条目保持未决，模
        型尝试保存在运行清单的 model_judgments 中。
        """
        request = {
            "step": STEP_INTERPRET,
            "execution_id": ctx.execution.execution_id,
            "question_ref": _ref_dict(question_ref),
            "rules": ctx.execution.input_snapshot.rules.version,
            "model": self._model_spec(ctx, step_id),
        }
        response = self._call_or_reuse(
            ctx,
            step_id=step_id,
            request=request,
            state=state,
            call_key=f"{STEP_INTERPRET}-{question_ref.record_id}",
        )
        if response.get("attempt") != "unresolved":
            raise ValueError(
                f"模拟服务返回了未知的解释尝试结果：{response.get('attempt')!r}"
            )
        append_run_revision(
            ctx.space,
            ctx.execution,
            model_judgment=ModelJudgment(
                step_id=step_id,
                judgment=str(response["note"]),
                created_at=ctx.now(),
            ),
        )

    # ---- 名单步骤 ----

    def _direct_roster(self, ctx: RuntimeContext, state: dict) -> dict:
        op_input = self._op_input(ctx, "direct_roster", state)
        digest = sha256_of(op_input)
        saved = ctx.reuse_step("direct_roster", digest)
        if saved is not None:
            refs = [_ref_dict(ref) for ref in saved.result_refs]
        else:
            response = self._call_or_reuse(
                ctx,
                step_id="direct_roster",
                request={"step": "direct_roster", **op_input},
                state=state,
                call_key="direct_roster",
            )
            refs = self._commit_roster(
                ctx,
                state,
                entries=response["entries"],
                method="direct",
                step_id="direct_roster",
                digest=digest,
            )
        return self._finish_roster_node(ctx, state, refs, "direct_roster")

    def _discover_candidates(self, ctx: RuntimeContext, state: dict) -> dict:
        op_input = self._op_input(ctx, "discover_candidates", state)
        digest = sha256_of(op_input)
        saved = ctx.reuse_step("discover_candidates", digest)
        if saved is not None:
            response = self._stored_response(
                ctx,
                "discover_candidates",
                {"step": "discover_candidates", **op_input},
            )
            if response is None:  # pragma: no cover - 已提交步骤必有归档
                response = {"candidates": state["material"]["candidates"]}
        else:
            response = self._call_or_reuse(
                ctx,
                step_id="discover_candidates",
                request={"step": "discover_candidates", **op_input},
                state=state,
                call_key="discover_candidates",
            )
            ctx.commit_step(
                step_id="discover_candidates",
                input_digest=digest,
                records=[],
                result_refs=[],
            )
        return {"candidates": response["candidates"]}

    def _review_candidates(self, ctx: RuntimeContext, state: dict) -> dict:
        base = self._op_input(ctx, "review_candidates", state)
        candidates = list(state["candidates"])
        op_input = {**base, "candidates": [c["candidate_id"] for c in candidates]}
        digest = sha256_of(op_input)
        saved = ctx.reuse_step("review_candidates", digest)
        if saved is not None:
            refs = [_ref_dict(ref) for ref in saved.result_refs]
        else:
            entries = []
            for candidate in candidates:
                request = {
                    "step": "review_candidates",
                    **base,
                    "candidate_id": candidate["candidate_id"],
                }
                response = self._call_or_reuse(
                    ctx,
                    step_id="review_candidates",
                    request=request,
                    state=state,
                    call_key=f"review_candidates-{candidate['candidate_id']}",
                )
                entries.append(response["entry"])
            refs = self._commit_roster(
                ctx,
                state,
                entries=entries,
                method="candidate_review",
                step_id="review_candidates",
                digest=digest,
            )
        return self._finish_roster_node(ctx, state, refs, "review_candidates")

    def _finish_roster_node(
        self, ctx: RuntimeContext, state: dict, refs: list[dict], step_id: str
    ) -> dict:
        roster_refs = [ref for ref in refs if not ref["record_id"].startswith("q-")]
        questions = _question_refs(refs)
        if state["expert_adjudication"] and questions:
            self._await_decisions(ctx, questions, step_id)
        return {
            "roster_id": roster_refs[0]["record_id"] if roster_refs else "",
            "roster_refs": roster_refs,
            "question_refs": questions,
        }

    def _commit_roster(
        self,
        ctx: RuntimeContext,
        state: dict,
        *,
        entries: list[dict],
        method: str,
        step_id: str,
        digest: str,
    ) -> list[dict]:
        """按服务响应构建名单记录；未决条目与问题在同一事务保存。"""
        from ..records.roster import RosterData, RosterEntry, RosterIdentifier

        execution_id = ctx.execution.execution_id
        spec = state["material"]
        paper_key = spec["paper_key"]
        roster_id = mock.roster_record_id(paper_key, execution_id)
        source = mock.source_ref(spec)
        rules = ctx.execution.input_snapshot.rules
        review = ReviewService(ctx.space)
        pending: list[tuple[dict, RecordRef]] = []
        roster_entries: list[RosterEntry] = []
        for entry in entries:
            evidence = mock.evidence_ref(spec, entry["evidence"]["line"])
            issue_refs: list[RecordRef] = []
            if entry["disposition"] == "pending":
                question_id = derive_question_id(
                    execution_id,
                    mock.inclusion_question(
                        paper_key,
                        roster_id=roster_id,
                        item_id=entry["item_id"],
                        source=source,
                        rules=rules,
                    ),
                    space_id=ctx.space.identity.space_id,
                )
                issue_refs = [RecordRef(record_id=question_id, revision=1)]
                pending.append((entry, issue_refs[0]))
            roster_entries.append(
                RosterEntry(
                    item_id=entry["item_id"],
                    identifiers=[
                        RosterIdentifier(
                            value=entry["identifier"], evidence_refs=[evidence]
                        )
                    ],
                    disposition=entry["disposition"],
                    evidence_refs=[evidence],
                    issue_refs=issue_refs,
                    contribution_id=entry.get("contribution_id"),
                )
            )
        roster = build_record(
            RosterRecord,
            record_id=roster_id,
            created_at=ctx.now(),
            execution_id=execution_id,
            data=RosterData(
                paper_key=paper_key,
                source_ref=source.model_dump(mode="json"),
                method=method,
                coverage={
                    "state": "complete",
                    "checked_file_ids": ["paper"],
                    "gap_refs": [],
                },
                entries=roster_entries,
                unresolved_groups=[],
                issue_refs=[],
            ),
        )
        result_refs = [roster.ref()]
        for entry, _ in pending:
            content = mock.inclusion_question(
                paper_key,
                roster_id=roster_id,
                item_id=entry["item_id"],
                source=source,
                rules=rules,
            )
            self.hook("before_question_create")
            view = review.create_question(
                execution_id=execution_id,
                step_id=step_id,
                request_key=f"{execution_id}:question:roster:{entry['item_id']}",
                content=content,
                records=[roster],
                expected_revisions={},
            )
            self.hook("after_question_create")
            result_refs.append(view.question_ref)
            if not state["expert_adjudication"] and self._pure_ai_allowed():
                self._attempt_interpretation(
                    ctx,
                    state=state,
                    step_id=step_id,
                    question_ref=view.question_ref,
                )
        ctx.commit_step(
            step_id=step_id,
            input_digest=digest,
            records=[roster],
            result_refs=result_refs,
        )
        return [_ref_dict(ref) for ref in result_refs]

    # ---- 数值与收尾 ----

    def _quantities(self, ctx: RuntimeContext, state: dict) -> dict:
        execution_id = ctx.execution.execution_id
        spec = state["material"]
        roster_id = state["roster_id"] or mock.roster_record_id(
            spec["paper_key"], execution_id
        )
        roster_rev = ctx.store.current_revision(roster_id)
        assert roster_rev is not None
        roster = ctx.store.read_record(
            RecordRef(record_id=roster_id, revision=roster_rev)
        )
        assert isinstance(roster, RosterRecord)
        included = [
            entry for entry in roster.data.entries if entry.disposition == "included"
        ]
        op_input = {
            **self._op_input(ctx, "quantities", state),
            "roster_ref": {"record_id": roster_id, "revision": roster_rev},
            "included_items": [entry.item_id for entry in included],
        }
        digest = sha256_of(op_input)
        saved = ctx.reuse_step("quantities", digest)
        if saved is not None:
            refs = [_ref_dict(ref) for ref in saved.result_refs]
        else:
            response = self._call_or_reuse(
                ctx,
                step_id="quantities",
                request={"step": "quantities", **op_input},
                state=state,
                call_key="quantities",
            )
            refs = self._commit_paper(
                ctx, state, roster=roster, response=response, digest=digest
            )
        questions = _question_refs(refs)
        if state["expert_adjudication"] and questions:
            self._await_decisions(ctx, questions, "quantities")
        contribution_refs = [
            ref for ref in refs if not ref["record_id"].startswith("q-")
        ]
        return {
            "paper_id": contribution_refs[0]["record_id"] if contribution_refs else "",
            "contribution_refs": contribution_refs,
            "question_refs": questions,
        }

    def _commit_paper(
        self,
        ctx: RuntimeContext,
        state: dict,
        *,
        roster: RosterRecord,
        response: dict,
        digest: str,
    ) -> list[dict]:
        """按名单实际内容校验数值响应并保存论文贡献记录。"""
        execution_id = ctx.execution.execution_id
        spec = state["material"]
        paper_key = spec["paper_key"]
        included_ids = {
            entry.item_id
            for entry in roster.data.entries
            if entry.disposition == "included"
        }
        contributions = list(response["contributions"])
        for item in contributions:
            if item["roster_item_id"] not in included_ids:
                raise ValueError(
                    "数值步骤收到被排除或未决的名单条目"
                    f" {item['roster_item_id']!r}；数值不接受排除或未决对象"
                )
        covered = {item["roster_item_id"] for item in contributions}
        missing = sorted(included_ids - covered)
        if missing:
            raise ValueError(
                f"数值响应缺少收录条目的贡献：{missing}；名单缺失或失败不能当作空成功"
            )
        needs_expert = bool(response["needs_expert"])
        paper_id = mock.paper_record_id(paper_key, execution_id)
        rules = ctx.execution.input_snapshot.rules
        question_ref: RecordRef | None = None
        if needs_expert:
            question_id = derive_question_id(
                execution_id,
                mock.author_assessment_question(
                    paper_key,
                    paper_id=paper_id,
                    source=mock.source_ref(spec),
                    rules=rules,
                ),
                space_id=ctx.space.identity.space_id,
            )
            question_ref = RecordRef(record_id=question_id, revision=1)
        paper = build_paper_record(
            paper_id=paper_id,
            spec=spec,
            roster_ref=roster.ref(),
            source_ref=mock.source_ref(spec),
            contributions=contributions,
            question_ref=question_ref,
            created_at=ctx.now(),
            execution_id=execution_id,
        )
        result_refs = [paper.ref()]
        if needs_expert:
            review = ReviewService(ctx.space)
            self.hook("before_question_create")
            view = review.create_question(
                execution_id=execution_id,
                step_id="quantities",
                request_key=f"{execution_id}:question:quantities:{paper_key}",
                content=mock.author_assessment_question(
                    paper_key,
                    paper_id=paper_id,
                    source=mock.source_ref(spec),
                    rules=rules,
                ),
                records=[paper],
                expected_revisions={},
            )
            self.hook("after_question_create")
            result_refs.append(view.question_ref)
            if not state["expert_adjudication"] and self._pure_ai_allowed():
                self._attempt_interpretation(
                    ctx,
                    state=state,
                    step_id="quantities",
                    question_ref=view.question_ref,
                )
        ctx.commit_step(
            step_id="quantities",
            input_digest=digest,
            records=[paper],
            result_refs=result_refs,
        )
        return [_ref_dict(ref) for ref in result_refs]

    def _finalize(self, ctx: RuntimeContext, state: dict) -> dict:
        execution_id = ctx.execution.execution_id
        paper_id = state["paper_id"] or mock.paper_record_id(
            state["paper_key"], execution_id
        )
        paper_rev = ctx.store.current_revision(paper_id)
        assert paper_rev is not None
        paper_ref = RecordRef(record_id=paper_id, revision=paper_rev)
        op_input = {
            **self._op_input(ctx, "finalize", state),
            "paper_ref": _ref_dict(paper_ref),
        }
        digest = sha256_of(op_input)
        saved = ctx.reuse_step("finalize", digest)
        if saved is None:
            objects = build_object_records(
                paper_ref=paper_ref,
                paper=ctx.store.read_record(paper_ref),
                created_at=ctx.now(),
                execution_id=execution_id,
            )
            ctx.commit_step(
                step_id="finalize",
                input_digest=digest,
                records=objects,
                result_refs=[obj.ref() for obj in objects],
            )
        paper = ctx.store.read_record(paper_ref)
        assert paper is not None
        return {
            "summary": {
                "paper_ref": _ref_dict(paper_ref),
                "object_ids": [
                    mock.object_record_id(
                        state["paper_key"],
                        contribution.identifiers[0].value,
                        execution_id,
                    )
                    for contribution in paper.data.object_contributions
                ],
            }
        }

    # ---- 决定应用与状态核对 ----

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


# ---- 记录构建（固定合成内容） ----


def _ev(spec: dict, line: int) -> dict:
    return mock.evidence_ref(spec, line).model_dump(mode="json")


def build_paper_record(
    *,
    paper_id: str,
    spec: dict,
    roster_ref: RecordRef,
    source_ref: RecordRef,
    contributions: list[dict],
    question_ref: RecordRef | None,
    created_at,
    execution_id: str,
):
    from ..records.paper import (
        MeasuredValue,
        ObjectContribution,
        PaperRecord,
    )
    from ..records.rules import quantity_paths

    built: list[ObjectContribution] = []
    for item in contributions:
        boundness = item["boundness"]
        if boundness["kind"] == "needs_expert":
            boundness_payload = {
                "status": None,
                "evidence_refs": [],
                "issue_refs": [question_ref.model_dump(mode="json")]
                if question_ref is not None
                else [],
                "gap_refs": [],
            }
            conditional = [
                {
                    "condition": entry["condition"],
                    "statement": entry["statement"],
                    "evidence_refs": [_ev(spec, entry["evidence_line"])],
                }
                for entry in boundness["conditionals"]
            ]
        else:
            boundness_payload = {
                "status": boundness["status"],
                "evidence_refs": [_ev(spec, boundness["evidence_line"])],
                "issue_refs": [],
                "gap_refs": [],
            }
            conditional = []
        groups: dict[str, list] = {}
        for value in item["values"]:
            groups.setdefault(value["quantity"], []).append(
                MeasuredValue.model_validate(
                    {
                        "value_id": value["value_id"],
                        "value": value["value"],
                        "error": value["error"],
                        "lower_error": None,
                        "upper_error": None,
                        "unit": value["unit"],
                        "limit_kind": "none",
                        "range_lower": None,
                        "range_upper": None,
                        "lower_inclusive": None,
                        "upper_inclusive": None,
                        "interval_kind": None,
                        "coverage": None,
                        "lower_quantile": None,
                        "upper_quantile": None,
                        "error_components": [],
                        "condition": value["condition"],
                        "frame": value["frame"],
                        "epoch": None,
                        "coordinate_format": None,
                        "representation": None,
                        "purpose": "measurement",
                        "source": "this_paper",
                        "source_note": None,
                        "author_usage": None,
                        "direct_evidence": [
                            {
                                "part": "value",
                                "evidence_ref": _ev(spec, value["evidence_line"]),
                                "raw_fragment": value["value"],
                            },
                            {
                                "part": "error",
                                "evidence_ref": _ev(spec, value["evidence_line"]),
                                "raw_fragment": value["error"],
                            },
                        ],
                        "context_evidence_refs": [],
                    }
                )
            )
        built.append(
            ObjectContribution.model_validate(
                {
                    "item_id": item["contribution_id"],
                    "roster_item_id": item["roster_item_id"],
                    "identifiers": [
                        {
                            "value": item["identifier"],
                            "evidence_refs": [
                                _ev(spec, item["identifier_evidence_line"])
                            ],
                        }
                    ],
                    "contribution_summary": {
                        "text": item["summary"],
                        "evidence_refs": [
                            _ev(spec, line) for line in item["summary_lines"]
                        ],
                    },
                    "contribution_type": {
                        "value": item["contribution_type"],
                        "evidence_refs": [_ev(spec, item["type_evidence_line"])],
                    },
                    "paper_boundness": boundness_payload,
                    "conditional_assessments": conditional,
                    "quantities": [
                        {
                            "quantity": quantity,
                            "values": [
                                value.model_dump(mode="json") for value in group_values
                            ],
                        }
                        for quantity, group_values in groups.items()
                    ],
                    "quantity_conflicts": [],
                    "quantity_coverage": [
                        {
                            "quantity": quantity,
                            "state": "complete",
                            "checked_file_ids": ["paper"],
                            "gap_refs": [],
                            "issue_refs": [],
                        }
                        for quantity in quantity_paths()
                    ],
                    "precedent_uses": [],
                }
            )
        )
    return build_record(
        PaperRecord,
        record_id=paper_id,
        created_at=created_at,
        execution_id=execution_id,
        data={
            "paper_key": spec["paper_key"],
            "metadata": {
                "arxiv_id": None,
                "version": "fictional-v1",
                "title": None,
                "authors": [],
            },
            "source_ref": source_ref.model_dump(mode="json"),
            "roster_ref": roster_ref.model_dump(mode="json"),
            "object_contributions": [
                contribution.model_dump(mode="json") for contribution in built
            ],
        },
    )


def build_object_records(
    *,
    paper_ref: RecordRef,
    paper,
    created_at,
    execution_id: str,
) -> list:
    """按论文记录的实际贡献构建对象记录；证据引用取自贡献本身。"""
    from ..records.objects import ContributionLink, ObjectData, ObjectRecord

    assert paper is not None
    objects = []
    for contribution in paper.data.object_contributions:
        identifier = contribution.identifiers[0]
        evidence = identifier.evidence_refs[0]
        objects.append(
            build_record(
                ObjectRecord,
                record_id=mock.object_record_id(
                    paper.data.paper_key, identifier.value, execution_id
                ),
                created_at=created_at,
                execution_id=execution_id,
                data=ObjectData(
                    object_id=(
                        f"fic-{_object_slug(paper.data.paper_key)}-"
                        f"{_object_slug(identifier.value)}"
                    ),
                    display_name=identifier.value,
                    aliases=[],
                    contribution_links=[
                        ContributionLink(
                            target_ref=paper_ref,
                            item_id=contribution.item_id,
                            state="confirmed",
                            basis=(
                                "Single contribution links this object to one paper."
                            ),
                            evidence_refs=[evidence],
                            decision_refs=[],
                        )
                    ],
                    external_links=[],
                    changes=[],
                ),
            )
        )
    return objects


def _object_slug(identifier: str) -> str:
    return identifier.lower().replace("_", "-")
