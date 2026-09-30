"""裁决共同接口：创建与查询待办、保存决定、替代问题。

事务、锁与恢复机制由工作空间与运行模块提供；本模块只负责问题与
决定的业务规则：绑定核对、指纹核对、请求重放、冲突与过期拒绝、
未决答复保留。接口供合成流程与 Python 调用；网页与正式裁决 CLI、
判例接入尚未提供。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ..records.common import (
    GapRef,
    ItemRef,
    RecordRef,
    canonical_json,
    sha256_of,
)
from ..records.questions import (
    Actor,
    DecisionData,
    DecisionPatch,
    DecisionRecord,
    QuestionRecord,
    RulesRef,
    compute_question_fingerprint,
)
from ..records.registry import AnyRecord
from ..records.runs import RunRecord
from ..workspace import (
    AdjudicationNotAllowedError,
    CommitRequest,
    DecisionConflictError,
    OperationKeyConflictError,
    RevisionStaleError,
    WorkspaceIntegrityError,
)

if TYPE_CHECKING:
    from ..workspace import Workspace

#: 决定保存回执的类型标记；应用阶段回执由运行层写入。
DECISION_SAVE_KIND = "decision_save"
QUESTION_CREATE_KIND = "question_create"
QUESTION_SUPERSEDE_KIND = "question_supersede"
DECISION_APPLY_KIND = "decision_apply"


@dataclass(frozen=True)
class QuestionContent:
    """问题的完整科学上下文；编号、修订与指纹由程序生成。"""

    category: str
    target_refs: list[ItemRef]
    source_refs: list[RecordRef]
    rules_ref: RulesRef
    prompt: str
    evidence_refs: list[RecordRef] = field(default_factory=list)
    gap_refs: list[GapRef] = field(default_factory=list)
    context_refs: list[RecordRef] = field(default_factory=list)

    def content_payload(self) -> dict:
        return {
            "category": self.category,
            "target_refs": [ref.model_dump(mode="json") for ref in self.target_refs],
            "source_refs": [ref.model_dump(mode="json") for ref in self.source_refs],
            "rules_ref": self.rules_ref.model_dump(mode="json"),
            "prompt": self.prompt,
            "evidence_refs": [
                ref.model_dump(mode="json") for ref in self.evidence_refs
            ],
            "gap_refs": [ref.model_dump(mode="json") for ref in self.gap_refs],
            "context_refs": [ref.model_dump(mode="json") for ref in self.context_refs],
        }


@dataclass(frozen=True)
class DecisionSubmission:
    """专家决定提交内容；重放比较以此为摘要，不含程序生成字段。"""

    question_ref: RecordRef
    fingerprint: str
    actor: Actor
    outcome: str
    answer_original: str
    answer_en: str
    reason: str
    evidence_refs: list[RecordRef] = field(default_factory=list)
    patch: DecisionPatch | None = None

    def content_payload(self) -> dict:
        return {
            "question_ref": self.question_ref.model_dump(mode="json"),
            "fingerprint": self.fingerprint,
            "actor": self.actor.model_dump(mode="json"),
            "outcome": self.outcome,
            "answer_original": self.answer_original,
            "answer_en": self.answer_en,
            "reason": self.reason,
            "evidence_refs": [
                ref.model_dump(mode="json") for ref in self.evidence_refs
            ],
            "patch": self.patch.model_dump(mode="json")
            if self.patch is not None
            else None,
        }


@dataclass
class ApplicationState:
    phase: str  # applied | blocked
    receipt_key: str
    result_refs: tuple[RecordRef, ...]
    error: str | None


@dataclass
class QuestionView:
    """问题的当前展示状态：记录、绑定、答复与应用进度。"""

    record: QuestionRecord
    question_ref: RecordRef
    execution_id: str
    step_id: str | None
    status: str  # open | answered | superseded
    answered_outcome: str | None
    application: ApplicationState | None
    superseded_by: RecordRef | None
    decisions: list[DecisionRecord]

    @property
    def needs_processing(self) -> bool:
        if self.status == "open":
            return True
        if self.status == "superseded":
            return False
        # answered：仍未决或待应用的问题仍需处理；已应用或受阻则不再等待
        if self.answered_outcome == "unresolved":
            return True
        return self.application is None


@dataclass
class DecisionReceipt:
    receipt: object  # workspace.Receipt
    decision_ref: RecordRef
    question_ref: RecordRef
    first_save: bool
    question_status: str
    note: str | None = None


def _ref(payload: object) -> RecordRef:
    return RecordRef.model_validate(payload)


def derive_question_id(
    execution_id: str, content: QuestionContent, *, space_id: str
) -> str:
    """按逻辑空间、执行与完整问题内容派生问题编号。

    执行编号是各空间独立的计数器，问题编号必须同时区别逻辑空间，
    否则两个空间对同一论文按相同顺序创建的问题会得到相同编号，决定
    请求可能串入另一空间。同一执行内同内容的问题只有一个身份（同请求
    重放或换请求键重建都返回同一条记录）；备份恢复保留逻辑空间身份，
    恢复空间内重算编号仍一致。调用方可据此在问题与它指向的目标记录仍
    不存在时预先组装互相引用的同批记录。
    """
    digest = sha256_of(
        {
            "space_id": space_id,
            "execution_id": execution_id,
            "content": content.content_payload(),
        }
    )[:12]
    return f"q-{execution_id}-{digest}"


def _decision_content_key(data: DecisionData) -> str:
    """同一问题版本内比较两份决定内容是否一致的键。"""
    return canonical_json(
        {
            "actor": data.actor.model_dump(mode="json"),
            "outcome": data.outcome,
            "answer_original": data.answer_original,
            "answer_en": data.answer_en,
            "reason": data.reason,
            "evidence_refs": [
                ref.model_dump(mode="json") for ref in data.evidence_refs
            ],
            "patch": data.result_patch.model_dump(mode="json")
            if data.result_patch is not None
            else None,
        }
    )


class ReviewService:
    """一个工作空间内的待办与决定服务。"""

    def __init__(self, space: Workspace) -> None:
        self.space = space

    # ---- 查询 ----

    def get_question(self, ref: RecordRef) -> QuestionView:
        record = self.space.store().read_record(ref)
        if record is None or not isinstance(record, QuestionRecord):
            raise WorkspaceIntegrityError(
                f"问题 {ref.record_id} 修订 {ref.revision} 不存在；查询使用准确修订引用"
            )
        return self._view(ref.record_id)

    def list_questions(
        self,
        *,
        execution_id: str | None = None,
        step_id: str | None = None,
        needs_processing: bool | None = None,
    ) -> list[QuestionView]:
        rows = self.space.store().list_questions(execution_id=execution_id)
        views = [self._view(row.question_id) for row in rows]
        if step_id is not None:
            views = [view for view in views if view.step_id == step_id]
        if needs_processing is not None:
            views = [
                view for view in views if view.needs_processing == needs_processing
            ]
        return views

    def get_decision(self, ref: RecordRef) -> DecisionRecord:
        record = self.space.store().read_record(ref)
        if not isinstance(record, DecisionRecord):
            raise WorkspaceIntegrityError(
                f"引用的记录不是决定：{ref.record_id} v{ref.revision}"
            )
        return record

    def find_application(self, decision_ref: RecordRef) -> ApplicationState | None:
        for row in self.space.store().find_receipts(kind=DECISION_APPLY_KIND):
            if row.links.get("decision_ref") == decision_ref.model_dump(mode="json"):
                phase = str(row.links.get("phase", "applied"))
                error = row.links.get("error")
                return ApplicationState(
                    phase=phase,
                    receipt_key=row.operation_key,
                    result_refs=row.result_refs,
                    error=str(error) if error else None,
                )
        return None

    # ---- 创建与提交 ----

    def create_question(
        self,
        *,
        execution_id: str,
        step_id: str,
        request_key: str,
        content: QuestionContent,
        records: list[AnyRecord] | None = None,
        expected_revisions: dict[str, int] | None = None,
    ) -> QuestionView:
        """保存问题及执行/步骤绑定；同请求同内容返回原问题。

        ``records`` 允许调用方把问题与它指向的目标记录在同一业务事务
        中提交（例如论文贡献与其歧义问题相互引用时）。问题的步骤绑定
        与记录、回执同事务保存：中断不会留下没有绑定的问题，重放直接
        返回原问题及其绑定。
        """
        with self.space.lock():
            store = self.space.store()
            payload = {
                "execution_id": execution_id,
                "step_id": step_id,
                "content": content.content_payload(),
                "records": [
                    [r.record_id, r.revision, r.RECORD_NAME] for r in (records or [])
                ],
                "expected_revisions": expected_revisions or {},
            }
            digest = sha256_of(payload)
            existing = store.find_receipt(request_key)
            if existing is not None:
                if existing.content_digest == digest:
                    return self._view(existing.links["question_ref"]["record_id"])
                raise OperationKeyConflictError(
                    f"操作键 {request_key!r} 已用于不同内容；"
                    "重试须携带相同内容，或为新内容更换操作键"
                )
            self._require_execution(execution_id)
            fingerprint = compute_question_fingerprint(
                category=content.category,
                target_refs=content.target_refs,
                evidence_refs=content.evidence_refs,
                source_refs=content.source_refs,
                rules_ref=content.rules_ref,
            )
            question_id = derive_question_id(
                execution_id, content, space_id=self.space.identity.space_id
            )
            question = QuestionRecord.model_validate(
                {
                    "schema": {"name": "question", "version": 1},
                    "record_id": question_id,
                    "revision": 1,
                    "created_at": datetime.now(UTC).isoformat(),
                    "execution_id": execution_id,
                    "data": {
                        "category": content.category,
                        "target_refs": [
                            ref.model_dump(mode="json") for ref in content.target_refs
                        ],
                        "source_refs": [
                            ref.model_dump(mode="json") for ref in content.source_refs
                        ],
                        "rules_ref": content.rules_ref.model_dump(mode="json"),
                        "prompt": content.prompt,
                        "evidence_refs": [
                            ref.model_dump(mode="json") for ref in content.evidence_refs
                        ],
                        "gap_refs": [
                            ref.model_dump(mode="json") for ref in content.gap_refs
                        ],
                        "context_refs": [
                            ref.model_dump(mode="json") for ref in content.context_refs
                        ],
                        "fingerprint": fingerprint,
                        "status": "open",
                        "decision_refs": [],
                    },
                }
            )
            request = CommitRequest(
                operation_key=request_key,
                kind=QUESTION_CREATE_KIND,
                new_records=[question, *(records or [])],
                expected_revisions=dict(expected_revisions or {}),
                execution_id=execution_id,
                step_id=step_id,
                links={"question_ref": question.ref().model_dump(mode="json")},
                content=payload,
                question_steps={question_id: step_id},
            )
            store.commit(request)
            return self._view(question_id)

    def submit_decision(
        self,
        *,
        execution_id: str,
        step_id: str,
        submission: DecisionSubmission,
        request_key: str,
    ) -> DecisionReceipt:
        """先校验适用性，再在同一业务事务保存决定与问题的答复修订。

        重放比较的内容不含程序管理的问题修订号；重放入口按原成功回执
        关联的修订单独核验提交引用（原修订或答复修订）。首次提交必须
        指向准确的问题修订（问题版本以指纹与修订共同核对），已答复问
        题只开放答复前后的最短修订链，同一问题版本换请求键提交相同内
        容也返回原决定。
        """
        with self.space.lock():
            store = self.space.store()
            payload = {
                "execution_id": execution_id,
                "step_id": step_id,
                "decision": {
                    **submission.content_payload(),
                    # 修订号由程序管理，重试时可能指向答复后的新修订
                    "question_ref": {"record_id": submission.question_ref.record_id},
                },
            }
            digest = sha256_of(payload)
            # 先查已成功的重放，再判问题当前状态：已成功的重复提交不误报为过期
            existing = store.find_receipt(request_key)
            if existing is not None:
                if existing.content_digest != digest:
                    raise OperationKeyConflictError(
                        f"操作键 {request_key!r} 已用于不同内容；"
                        "重试须携带相同内容，或为新内容更换操作键"
                    )
                # 重放同样核验提交引用：只承认原成功回执明确关联的原修订
                # 与答复修订，不存在或无关的修订不构成同一请求
                original = _ref(existing.links["question_ref_before"])
                answered = _ref(existing.links["question_ref_after"])
                if submission.question_ref.revision not in {
                    original.revision,
                    answered.revision,
                }:
                    raise RevisionStaleError(
                        f"重放提交指向问题 {submission.question_ref.record_id}"
                        f" 修订 {submission.question_ref.revision}，原成功回执"
                        f" 关联的修订为 {original.revision}"
                        f"（答复后 {answered.revision}）；"
                        "不存在或无关的修订不能按同一请求重放"
                    )
                decision_ref = _ref(existing.links["decision_ref"])
                question_ref = _ref(existing.links["question_ref_after"])
                view = self._view(question_ref.record_id)
                note = None
                if view.status == "superseded":
                    note = "该问题随后已被替代：回执仍然有效，但决定不可再次应用"
                return DecisionReceipt(
                    receipt=existing.to_receipt(replayed=True),
                    decision_ref=decision_ref,
                    question_ref=question_ref,
                    first_save=False,
                    question_status=view.status,
                    note=note,
                )

            question = self._binding_checked_question(
                execution_id=execution_id,
                step_id=step_id,
                question_ref=submission.question_ref,
            )
            if question.data.status == "superseded":
                raise RevisionStaleError(
                    f"问题 {question.record_id} 已被替代；"
                    "旧决定只保留历史，不能应用到替代后的新问题"
                )
            if submission.fingerprint != question.data.fingerprint:
                raise RevisionStaleError(
                    f"提交的指纹与问题 {question.record_id} 当前内容不一致；"
                    "问题内容或目标修订已变化，过期提交被拒绝"
                )
            self._require_exact_revision(submission.question_ref, question)
            self._require_expert_adjudication(execution_id)
            if submission.actor.kind != "human":
                raise AdjudicationNotAllowedError(
                    "专家决定接口只接受人类操作者；纯 AI 合成处理使用"
                    " RunRecord.model_judgments 保存"
                )
            if submission.outcome not in ("resolved", "unresolved"):
                raise WorkspaceIntegrityError(f"未知决定结果：{submission.outcome!r}")
            if submission.outcome == "unresolved" and submission.patch is not None:
                raise WorkspaceIntegrityError(
                    "仍未决的答复不能携带结果补丁；保留未决时不应用任何修改"
                )

            if question.data.status == "answered":
                # 同一问题版本：相同内容返回原决定，不同内容拒绝
                submission_key = _decision_content_key(
                    _submission_as_data(question, submission)
                )
                for decision_ref in question.data.decision_refs:
                    decision = self.get_decision(decision_ref)
                    if _decision_content_key(decision.data) == submission_key:
                        return self._existing_decision_receipt(decision.ref(), question)
                    raise DecisionConflictError(
                        f"问题 {question.record_id}（修订 {question.revision}）"
                        f"已保存决定 {decision.record_id}，"
                        "本次提交内容与之冲突，被拒绝"
                    )
                raise DecisionConflictError(  # pragma: no cover - 校验保证非空
                    f"问题 {question.record_id} 已有答复，本次提交被拒绝"
                )

            decision_id = self._next_id("dec")
            answered_revision = question.revision + 1
            decided_at = datetime.now(UTC)
            decision = DecisionRecord.model_validate(
                {
                    "schema": {"name": "decision", "version": 1},
                    "record_id": decision_id,
                    "revision": 1,
                    "created_at": decided_at.isoformat(),
                    "execution_id": execution_id,
                    "data": {
                        "question_ref": {
                            "record_id": question.record_id,
                            "revision": answered_revision,
                        },
                        "question_fingerprint": question.data.fingerprint,
                        "actor": submission.actor.model_dump(mode="json"),
                        "decided_at": decided_at.isoformat(),
                        "outcome": submission.outcome,
                        "answer_original": submission.answer_original,
                        "answer_en": submission.answer_en,
                        "reason": submission.reason,
                        "evidence_refs": [
                            ref.model_dump(mode="json")
                            for ref in submission.evidence_refs
                        ],
                        "result_patch": submission.patch.model_dump(mode="json")
                        if submission.patch is not None
                        else None,
                    },
                }
            )
            answered = question.model_copy(
                update={
                    "revision": answered_revision,
                    "created_at": decided_at,
                    "data": question.data.model_copy(
                        update={
                            "status": "answered",
                            "decision_refs": [decision.ref()],
                        }
                    ),
                }
            )
            request = CommitRequest(
                operation_key=request_key,
                kind=DECISION_SAVE_KIND,
                new_records=[decision, answered],
                expected_revisions={question.record_id: question.revision},
                execution_id=execution_id,
                step_id=step_id,
                links={
                    "decision_ref": decision.ref().model_dump(mode="json"),
                    "question_ref_before": question.ref().model_dump(mode="json"),
                    "question_ref_after": answered.ref().model_dump(mode="json"),
                    "phase": "saved",
                },
                content=payload,
            )
            receipt = store.commit(request)
            return DecisionReceipt(
                receipt=receipt,
                decision_ref=decision.ref(),
                question_ref=answered.ref(),
                first_save=True,
                question_status="answered",
            )

    def supersede_question(
        self,
        *,
        execution_id: str,
        step_id: str,
        old_ref: RecordRef,
        request_key: str,
        new_content: QuestionContent,
        reason: str,
    ) -> QuestionView:
        """为变化后的问题建立新记录，旧问题追加失效修订；旧答复保留。"""
        with self.space.lock():
            store = self.space.store()
            payload = {
                "execution_id": execution_id,
                "step_id": step_id,
                "old_ref": old_ref.model_dump(mode="json"),
                "reason": reason,
                "new_content": new_content.content_payload(),
            }
            digest = sha256_of(payload)
            existing = store.find_receipt(request_key)
            if existing is not None:
                if existing.content_digest == digest:
                    return self._view(existing.links["new_question_ref"]["record_id"])
                raise OperationKeyConflictError(
                    f"操作键 {request_key!r} 已用于不同内容"
                )
            old = self._binding_checked_question(
                execution_id=execution_id, step_id=step_id, question_ref=old_ref
            )
            if old.data.status == "superseded":
                raise RevisionStaleError(f"问题 {old.record_id} 已被替代，不能再次替代")
            if old_ref.revision != old.revision:
                raise RevisionStaleError(
                    f"替代提交指向问题 {old.record_id} 修订 {old_ref.revision}，"
                    f"当前有效修订为 {old.revision}；只能替代当前修订"
                )
            fingerprint = compute_question_fingerprint(
                category=new_content.category,
                target_refs=new_content.target_refs,
                evidence_refs=new_content.evidence_refs,
                source_refs=new_content.source_refs,
                rules_ref=new_content.rules_ref,
            )
            new_id = derive_question_id(
                execution_id, new_content, space_id=self.space.identity.space_id
            )
            decided_at = datetime.now(UTC)
            new_question = QuestionRecord.model_validate(
                {
                    "schema": {"name": "question", "version": 1},
                    "record_id": new_id,
                    "revision": 1,
                    "created_at": decided_at.isoformat(),
                    "execution_id": execution_id,
                    "data": {
                        "category": new_content.category,
                        "target_refs": [
                            ref.model_dump(mode="json")
                            for ref in new_content.target_refs
                        ],
                        "source_refs": [
                            ref.model_dump(mode="json")
                            for ref in new_content.source_refs
                        ],
                        "rules_ref": new_content.rules_ref.model_dump(mode="json"),
                        "prompt": new_content.prompt,
                        "evidence_refs": [
                            ref.model_dump(mode="json")
                            for ref in new_content.evidence_refs
                        ],
                        "gap_refs": [
                            ref.model_dump(mode="json") for ref in new_content.gap_refs
                        ],
                        "context_refs": [
                            ref.model_dump(mode="json")
                            for ref in new_content.context_refs
                        ],
                        "fingerprint": fingerprint,
                        "status": "open",
                        "decision_refs": [],
                    },
                }
            )
            superseded = old.model_copy(
                update={
                    "revision": old.revision + 1,
                    "created_at": decided_at,
                    "data": old.data.model_copy(update={"status": "superseded"}),
                }
            )
            request = CommitRequest(
                operation_key=request_key,
                kind=QUESTION_SUPERSEDE_KIND,
                new_records=[superseded, new_question],
                expected_revisions={old.record_id: old.revision},
                execution_id=execution_id,
                step_id=step_id,
                links={
                    "old_question_ref_before": old.ref().model_dump(mode="json"),
                    "old_question_ref_after": superseded.ref().model_dump(mode="json"),
                    "new_question_ref": new_question.ref().model_dump(mode="json"),
                    "reason": reason,
                },
                content=payload,
                question_steps={new_id: step_id},
            )
            store.commit(request)
            return self._view(new_id)

    # ---- 内部 ----

    def _next_id(self, prefix: str) -> str:
        with self.space.store().transaction() as tx:
            return tx.next_id(prefix)

    def _require_exact_revision(
        self, question_ref: RecordRef, latest: QuestionRecord
    ) -> None:
        """首次提交必须指向仍然适用的准确修订。

        已答复的问题只开放明确的最短答复链：答复前后的相邻修订；其余
        旧修订或未知修订均按过期拒绝。重放走原成功回执，回放入口按回
        承关联的修订另行核验，不受当前修订限制。
        """
        if question_ref.revision == latest.revision:
            return
        if (
            latest.data.status == "answered"
            and question_ref.revision == latest.revision - 1
        ):
            return  # 答复前修订的合法重试（同版本内容比对在后续进行）
        raise RevisionStaleError(
            f"提交指向问题 {question_ref.record_id} 修订 {question_ref.revision}，"
            f"当前有效修订为 {latest.revision}；过期或未知修订被拒绝"
        )

    def _require_execution(self, execution_id: str):
        row = self.space.store().get_execution(execution_id)
        if row is None:
            raise WorkspaceIntegrityError(
                f"执行 {execution_id} 不存在于本空间；跨空间提交被拒绝"
            )
        return row

    def _require_expert_adjudication(self, execution_id: str) -> RunRecord:
        run = self.space.store().read_record_current(
            self._require_execution(execution_id).run_record_id
        )
        if not isinstance(run, RunRecord):
            raise WorkspaceIntegrityError(f"执行 {execution_id} 的运行清单缺失")
        if not run.data.input_snapshot.expert_adjudication:
            raise AdjudicationNotAllowedError(
                f"执行 {execution_id} 关闭了运行中专家裁决；"
                "本轮专家决定及其衍生结果不被接受"
            )
        return run

    def _binding_checked_question(
        self, *, execution_id: str, step_id: str, question_ref: RecordRef
    ) -> QuestionRecord:
        """核对提交指向的问题属于本执行与步骤，返回其当前修订。

        引用的准确修订必须存在；绑定缺失不视为通配——问题创建时绑定
        与记录同事务保存，缺失即数据不一致。指定的修订号是否仍适用由
        调用方按状态与答复链核对。
        """
        store = self.space.store()
        row = next(
            (
                row
                for row in store.list_questions(execution_id=execution_id)
                if row.question_id == question_ref.record_id
            ),
            None,
        )
        if row is None:
            raise WorkspaceIntegrityError(
                f"问题 {question_ref.record_id} 不属于执行 {execution_id}；"
                "跨空间、跨执行或跨步骤提交被拒绝"
            )
        referenced = store.read_record(question_ref)
        if referenced is None or not isinstance(referenced, QuestionRecord):
            raise WorkspaceIntegrityError(
                f"问题 {question_ref.record_id} 修订 {question_ref.revision}"
                " 不存在；提交必须使用准确的问题修订引用"
            )
        latest = store.read_record_current(question_ref.record_id)
        if not isinstance(latest, QuestionRecord):
            raise WorkspaceIntegrityError(
                f"引用的记录不是问题：{question_ref.record_id}"
            )
        if row.step_id != step_id:
            raise WorkspaceIntegrityError(
                f"问题 {question_ref.record_id} 属于步骤 {row.step_id!r}，"
                f"本次提交声明步骤 {step_id!r}；跨步骤或缺失绑定提交被拒绝"
            )
        return latest

    def _view(self, question_id: str) -> QuestionView:
        store = self.space.store()
        rows = store.list_questions()
        row = next((row for row in rows if row.question_id == question_id), None)
        if row is None:
            raise WorkspaceIntegrityError(f"未知问题：{question_id}")
        latest = store.read_record_current(question_id)
        if not isinstance(latest, QuestionRecord):  # pragma: no cover - 防御
            raise WorkspaceIntegrityError(f"问题 {question_id} 记录损坏")
        decisions = [self.get_decision(ref) for ref in latest.data.decision_refs]
        answered_outcome = decisions[-1].data.outcome if decisions else None
        application: ApplicationState | None = None
        for decision in decisions:
            state = self.find_application(decision.ref())
            if state is not None:
                application = state
                break
        superseded_by = None
        for receipt in store.find_receipts(kind=QUESTION_SUPERSEDE_KIND):
            if (
                receipt.links.get("old_question_ref_after", {}).get("record_id")
                == question_id
            ):
                superseded_by = _ref(receipt.links["new_question_ref"])
        return QuestionView(
            record=latest,
            question_ref=latest.ref(),
            execution_id=row.execution_id,
            step_id=row.step_id,
            status=latest.data.status,
            answered_outcome=answered_outcome,
            application=application,
            superseded_by=superseded_by,
            decisions=decisions,
        )

    def _existing_decision_receipt(
        self, decision_ref: RecordRef, question: QuestionRecord
    ) -> DecisionReceipt:
        for row in self.space.store().find_receipts(kind=DECISION_SAVE_KIND):
            if row.links.get("decision_ref") == decision_ref.model_dump(mode="json"):
                view = self._view(question.record_id)
                note = None
                if view.status == "superseded":
                    note = "该问题随后已被替代：决定只保留历史，不可再次应用"
                return DecisionReceipt(
                    receipt=row.to_receipt(replayed=True),
                    decision_ref=decision_ref,
                    question_ref=_ref(row.links["question_ref_after"]),
                    first_save=False,
                    question_status=view.status,
                    note=note,
                )
        raise WorkspaceIntegrityError(  # pragma: no cover - 数据应一致
            f"决定 {decision_ref.record_id} 缺少保存回执"
        )


def _submission_as_data(
    question: QuestionRecord, submission: DecisionSubmission
) -> DecisionData:
    """把提交内容包装成可比较的 DecisionData（时间戳为占位）。"""
    return DecisionData.model_validate(
        {
            "question_ref": question.ref().model_dump(mode="json"),
            "question_fingerprint": submission.fingerprint,
            "actor": submission.actor.model_dump(mode="json"),
            "decided_at": datetime.now(UTC).isoformat(),
            "outcome": submission.outcome,
            "answer_original": submission.answer_original,
            "answer_en": submission.answer_en,
            "reason": submission.reason,
            "evidence_refs": [
                ref.model_dump(mode="json") for ref in submission.evidence_refs
            ],
            "result_patch": submission.patch.model_dump(mode="json")
            if submission.patch is not None
            else None,
        }
    )
