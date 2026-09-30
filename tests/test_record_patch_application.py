"""补丁应用纯函数：校验与应用共用解释，只增不覆盖（A13 基础）。

集合校验（``validate_collection``）与实际应用（运行层）走同一
目标定位与字段约束；这里的用例同时在两条入口上核对。
"""

from __future__ import annotations

import pytest
from records_helpers import STAMP, contribution

from hvs_catalog.records import (
    CollectionValidationError,
    DecisionPatch,
    PaperRecord,
    PatchApplicationError,
    PatchChange,
    apply_decision_patch,
    build_record,
    validate_collection,
)


def _paper(contrib=None) -> PaperRecord:
    return build_record(
        PaperRecord,
        record_id="paper-x",
        created_at=STAMP,
        execution_id="exec-x",
        data={
            "paper_key": "SYNTHETIC-X",
            "metadata": {
                "arxiv_id": None,
                "version": "v1",
                "title": None,
                "authors": [],
            },
            "source_ref": {"record_id": "src-x", "revision": 1},
            "roster_ref": {"record_id": "roster-x", "revision": 1},
            "object_contributions": [
                (contrib or contribution()).model_dump(mode="json")
            ],
        },
    )


def _boundness_patch(*, order_reversed=False, record_id="paper-x", revision=1):
    changes = [
        PatchChange(
            field="paper_boundness",
            value={
                "status": "no_overall_conclusion",
                "evidence_refs": [],
                "issue_refs": [],
                "gap_refs": [],
            },
        ),
    ]
    return DecisionPatch(
        target={
            "record_id": record_id,
            "revision": revision,
            "item_id": "contribution-A",
        },
        changes=list(reversed(changes)) if order_reversed else changes,
    )


def test_apply_patch_returns_copy_with_same_revision_and_keeps_input():
    paper = _paper()
    patch = _boundness_patch()
    patched = apply_decision_patch(paper, patch)
    assert patched is not paper
    assert patched.revision == paper.revision
    assert paper.data.object_contributions[0].paper_boundness.status == "bound"
    assert (
        patched.data.object_contributions[0].paper_boundness.status
        == "no_overall_conclusion"
    )


def test_field_order_reversed_produces_same_result():
    paper = _paper()
    forward = apply_decision_patch(paper, _boundness_patch())
    reversed_ = apply_decision_patch(paper, _boundness_patch(order_reversed=True))
    assert forward == reversed_


def test_unknown_field_rejected():
    paper = _paper()
    patch = DecisionPatch(
        target={"record_id": "paper-x", "revision": 1, "item_id": "contribution-A"},
        changes=[PatchChange(field="record_id", value="hijack")],
    )
    with pytest.raises(PatchApplicationError, match="记录级字段"):
        apply_decision_patch(paper, patch)


def test_missing_item_and_unsupported_record_rejected():
    paper = _paper()
    patch = DecisionPatch(
        target={"record_id": "paper-x", "revision": 1, "item_id": "missing-item"},
        changes=[PatchChange(field="paper_boundness", value={})],
    )
    with pytest.raises(PatchApplicationError, match="不在"):
        apply_decision_patch(paper, patch)
    from hvs_catalog.records.roster import RosterRecord

    roster = build_record(
        RosterRecord,
        record_id="roster-x",
        created_at=STAMP,
        execution_id=None,
        data={
            "paper_key": "SYNTHETIC-X",
            "source_ref": {"record_id": "src-x", "revision": 1},
            "method": "direct",
            "coverage": {
                "state": "complete",
                "checked_file_ids": ["main"],
                "gap_refs": [],
            },
            "entries": [],
            "unresolved_groups": [],
            "issue_refs": [],
        },
    )
    with pytest.raises(PatchApplicationError, match="不支持条目级修改|不在"):
        apply_decision_patch(
            roster,
            DecisionPatch(
                target={"record_id": "roster-x", "revision": 1, "item_id": "a"},
                changes=[PatchChange(field="method", value="direct")],
            ),
        )


def test_validation_and_application_share_field_constraints():
    """校验拒绝的补丁，应用同样拒绝（两套入口一个解释）。"""
    paper = _paper()
    bad_patch = DecisionPatch(
        target={"record_id": "paper-x", "revision": 1, "item_id": "contribution-A"},
        changes=[PatchChange(field="paper_boundness", value={"status": "not_a_state"})],
    )
    with pytest.raises(PatchApplicationError, match="约束"):
        apply_decision_patch(paper, bad_patch)


def test_collection_validation_enforces_patch_scope(tmp_path):
    """集合校验继续核对补丁范围与指纹（复用共同纯函数）。"""
    from hvs_catalog.records import DecisionRecord, QuestionRecord, build_record

    paper = _paper()
    question = build_record(
        QuestionRecord,
        record_id="q-x",
        created_at=STAMP,
        execution_id=None,
        data={
            "category": "author_assessment",
            "target_refs": [
                {"record_id": "paper-x", "revision": 1, "item_id": "contribution-A"}
            ],
            "source_refs": [{"record_id": "src-x", "revision": 1}],
            "rules_ref": {"version": "0.1.0", "sha256": "0" * 64},
            "prompt": "Question",
            "evidence_refs": [],
            "gap_refs": [],
            "context_refs": [],
            "fingerprint": "1" * 64,
            "status": "open",
            "decision_refs": [],
        },
    )
    decision = build_record(
        DecisionRecord,
        record_id="dec-x",
        created_at=STAMP,
        execution_id=None,
        data={
            "question_ref": {"record_id": "q-x", "revision": 1},
            "question_fingerprint": "1" * 64,
            "actor": {"kind": "human", "id": "expert"},
            "decided_at": STAMP.isoformat(),
            "outcome": "resolved",
            "answer_original": "答复",
            "answer_en": "Answer",
            "reason": "Reason",
            "evidence_refs": [],
            "result_patch": {
                "target": {
                    "record_id": "paper-x",
                    "revision": 1,
                    "item_id": "contribution-OTHER",
                },
                "changes": [{"field": "paper_boundness", "value": {"status": "bound"}}],
            },
        },
    )
    with pytest.raises(CollectionValidationError, match="目标范围"):
        validate_collection([paper, question, decision])
