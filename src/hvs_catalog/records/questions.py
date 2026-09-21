"""科学疑问、专家决定与问题指纹。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .common import (
    Config,
    GapRef,
    ItemRef,
    Record,
    RecordRef,
    canonical_json,
    sha256_of,
)

QuestionCategory = Literal[
    "inclusion",
    "identity",
    "author_assessment",
    "numeric_conflict",
    "quantity_attribution",
    "interpretation",
]
QuestionStatus = Literal["open", "answered", "superseded"]

FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class RulesRef(Config):
    version: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class QuestionData(Config):
    category: QuestionCategory
    target_refs: list[ItemRef] = Field(min_length=1)
    source_refs: list[RecordRef] = Field(min_length=1)
    rules_ref: RulesRef
    prompt: str = Field(min_length=1)
    evidence_refs: list[RecordRef] = Field(min_length=0)
    gap_refs: list[GapRef] = Field(min_length=0)
    context_refs: list[RecordRef] = Field(min_length=0)
    fingerprint: str = Field(pattern=FINGERPRINT_PATTERN)
    status: QuestionStatus
    decision_refs: list[RecordRef] = Field(min_length=0)

    @model_validator(mode="after")
    def _status_matches_decisions(self) -> QuestionData:
        if self.status == "answered" and not self.decision_refs:
            raise ValueError("已答复的问题必须引用决定")
        if self.status == "open" and self.decision_refs:
            raise ValueError("未答复的问题不能引用决定")
        return self


def compute_question_fingerprint(
    *,
    category: str,
    target_refs: list[ItemRef],
    evidence_refs: list[RecordRef],
    source_refs: list[RecordRef],
    rules_ref: RulesRef,
) -> str:
    """由实际目标、证据、材料和规则计算指纹，不依赖显示文字。"""
    return sha256_of(
        {
            "category": category,
            "targets": [ref.model_dump(mode="json") for ref in target_refs],
            "evidence": [ref.model_dump(mode="json") for ref in evidence_refs],
            "sources": [ref.model_dump(mode="json") for ref in source_refs],
            "rules": rules_ref.model_dump(mode="json"),
        }
    )


class Actor(Config):
    kind: Literal["human", "model"]
    id: str = Field(min_length=1)


class PatchChange(Config):
    field: str = Field(min_length=1)
    value: object


FORBIDDEN_PATCH_KEYS = frozenset({"command", "shell", "exec", "script", "subprocess"})


class DecisionPatch(Config):
    target: ItemRef
    changes: list[PatchChange] = Field(min_length=1)

    @model_validator(mode="after")
    def _no_executable_content(self) -> DecisionPatch:
        serialized = canonical_json(self.model_dump(mode="json"))
        lowered = serialized.lower()
        for key in FORBIDDEN_PATCH_KEYS:
            if f'"{key}"' in lowered:
                raise ValueError(f"结果补丁不能包含可执行内容：{key}")
        return self


class DecisionData(Config):
    question_ref: RecordRef
    question_fingerprint: str = Field(pattern=FINGERPRINT_PATTERN)
    actor: Actor
    decided_at: datetime
    outcome: Literal["resolved", "unresolved"]
    answer_original: str = Field(min_length=1)
    answer_en: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence_refs: list[RecordRef] = Field(min_length=0)
    result_patch: DecisionPatch | None

    @model_validator(mode="after")
    def _timestamped(self) -> DecisionData:
        if self.decided_at.tzinfo is None:
            raise ValueError("decided_at 必须带时区")
        return self


class QuestionRecord(Record[QuestionData]):
    RECORD_NAME = "question"


class DecisionRecord(Record[DecisionData]):
    RECORD_NAME = "decision"
