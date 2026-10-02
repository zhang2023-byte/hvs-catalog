"""Shared review interfaces create/query pending questions, save decisions, and supersede
questions.

Workspace/runtime provide transactions/locks/recovery. This module owns bindings,
fingerprints, replay, conflicting/stale rejection, and retained unresolved answers.
Synthetic flows/Python use it; web, official review CLI, and precedents are not yet
provided.
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

#: Decision-save receipt marker; runtime writes application receipts separately.
DECISION_SAVE_KIND = "decision_save"
QUESTION_CREATE_KIND = "question_create"
QUESTION_SUPERSEDE_KIND = "question_supersede"
DECISION_APPLY_KIND = "decision_apply"


@dataclass(frozen=True)
class QuestionContent:
    """Complete scientific question context; programs derive identities, revisions, and
    fingerprints.
    """

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
    """Expert submission contents used for replay digests, excluding program-generated
    fields.
    """

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
    """Current question display state: records, bindings, answers, and application
    progress.
    """

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
        # Answered but unresolved/unapplied questions still need handling; applied/blocked questions stop waiting.
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
    """Derive question IDs from logical workspace, execution, and complete contents.

    Execution counters are workspace-local, so workspace identity prevents identical
    paper/order questions from sharing IDs and receiving foreign decisions. Identical
    contents within one execution yield one identity across replays/new keys. Backup
    preserves logical identity and recomputation. Callers may preassemble mutually
    referencing question/target records before either exists.
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
    """Key comparing decision contents within one question version."""
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
    """Workspace pending-question/decision service."""

    def __init__(self, space: Workspace) -> None:
        self.space = space

    # ---- Queries ----

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

    # ---- Creation and submission ----

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
        """Save questions with execution/step bindings; identical request/content returns
        the original.

        records permits mutually referencing target/question records in one business
        transaction. Save bindings, records, and receipts together so interruption
        leaves no unbound question; replay returns original questions/bindings.
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

    def create_questions(
        self,
        *,
        execution_id: str,
        step_id: str,
        request_key: str,
        questions: list[QuestionRecord],
        records: list[AnyRecord],
    ) -> list[QuestionView]:
        """Save mutually referencing step results and question bindings atomically.

        Callers assemble IDs with derive_question_id using complete QuestionContent.
        Batch creation has the same fixed-context and replay boundary as creation of
        one question; it never creates unbound question records.
        """
        with self.space.lock():
            self._require_execution(execution_id)
            ids = [q.record_id for q in questions]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate question identity in batch")
            for question in questions:
                data = question.data
                content = QuestionContent(
                    category=data.category,
                    target_refs=data.target_refs,
                    source_refs=data.source_refs,
                    rules_ref=data.rules_ref,
                    prompt=data.prompt,
                    evidence_refs=data.evidence_refs,
                    gap_refs=data.gap_refs,
                    context_refs=data.context_refs,
                )
                if (
                    question.execution_id != execution_id
                    or question.revision != 1
                    or data.status != "open"
                    or data.decision_refs
                    or question.record_id
                    != derive_question_id(
                        execution_id, content, space_id=self.space.identity.space_id
                    )
                ):
                    raise ValueError(
                        "question batch requires new, correctly derived fixed-context questions"
                    )
            self.space.store().commit(
                CommitRequest(
                    operation_key=request_key,
                    kind=QUESTION_CREATE_KIND,
                    new_records=[*records, *questions],
                    execution_id=execution_id,
                    step_id=step_id,
                    result_refs=[q.ref() for q in questions],
                    question_steps={qid: step_id for qid in ids},
                    content={
                        "execution_id": execution_id,
                        "step_id": step_id,
                        "questions": [
                            q.data.model_dump(mode="json") for q in questions
                        ],
                        "records": [
                            r.model_dump(
                                mode="json", by_alias=True, exclude={"created_at"}
                            )
                            for r in records
                        ],
                    },
                )
            )
            return [self._view(qid) for qid in ids]

    def submit_decision(
        self,
        *,
        execution_id: str,
        step_id: str,
        submission: DecisionSubmission,
        request_key: str,
    ) -> DecisionReceipt:
        """Validate applicability, then save decisions and answered-question revisions
        atomically.

        Replay content excludes program-managed revisions, while replay independently
        verifies original/answered revisions linked by successful receipts. Initial
        submissions target exact revisions validated by fingerprint/revision. Answered
        questions expose only the minimal adjacent answer chain; new-key identical
        content still returns original decisions.
        """
        with self.space.lock():
            store = self.space.store()
            payload = {
                "execution_id": execution_id,
                "step_id": step_id,
                "decision": {
                    **submission.content_payload(),
                    # Programs manage revisions; retries may reference the post-answer revision.
                    "question_ref": {"record_id": submission.question_ref.record_id},
                },
            }
            digest = sha256_of(payload)
            # Look up successful replay before current-state checks so successful duplicates are not falsely stale.
            existing = store.find_receipt(request_key)
            if existing is not None:
                if existing.content_digest != digest:
                    raise OperationKeyConflictError(
                        f"操作键 {request_key!r} 已用于不同内容；"
                        "重试须携带相同内容，或为新内容更换操作键"
                    )
                # Replay verifies references too: only original/answered revisions explicitly linked by successful receipts
                # belong to the same request; absent/unrelated revisions do not.
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
                # Identical contents within one question version return the original; differing contents reject.
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
                raise DecisionConflictError(  # pragma: no cover - validation ensures nonempty
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
        """Create replacement questions and superseding old revisions while retaining old
        answers.
        """
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

    # ---- Internal helpers ----

    def _next_id(self, prefix: str) -> str:
        with self.space.store().transaction() as tx:
            return tx.next_id(prefix)

    def _require_exact_revision(
        self, question_ref: RecordRef, latest: QuestionRecord
    ) -> None:
        """Initial submissions must target still-applicable exact revisions.

        Answered questions accept only adjacent pre/post-answer revisions; reject other
        old/unknown revisions as stale. Replay uses original successful receipts and
        verifies their linked revisions independently of current revision.
        """
        if question_ref.revision == latest.revision:
            return
        if (
            latest.data.status == "answered"
            and question_ref.revision == latest.revision - 1
        ):
            return  # Valid retry on the pre-answer revision; compare same-version contents later.
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
        """Verify question ownership by execution/step and return current revisions.

        Exact referenced revisions must exist; missing bindings are inconsistencies,
        never wildcards. Creation saves bindings atomically. Callers judge requested
        revision applicability through state/answer chains.
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
        if not isinstance(latest, QuestionRecord):  # pragma: no cover - defensive check
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
        raise WorkspaceIntegrityError(  # pragma: no cover - data should be consistent
            f"决定 {decision_ref.record_id} 缺少保存回执"
        )


def _submission_as_data(
    question: QuestionRecord, submission: DecisionSubmission
) -> DecisionData:
    """Wrap submissions as comparable DecisionData using placeholder timestamps."""
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
