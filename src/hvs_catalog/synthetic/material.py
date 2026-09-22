"""R05 合成流程的虚构论文材料：固定输入、证据与记录构造。

两篇虚构论文：FIC-R05-A 的作者在不同模型条件下给出相反结论且未给
总体判断（存在作者判断歧义）；FIC-R05-B 含义明确。科学内容全部固定
在本模块，不由运行时推断；全部标识（FIC-*）为虚构，不是真实论文、
Gold 或正式判例。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from ..records.common import ItemRef, RecordRef, build_record
from ..records.questions import RulesRef

PaperId = Literal["A", "B"]

PAPER_KEYS = {"A": "FIC-R05-A", "B": "FIC-R05-B"}
SOURCE_IDS = {"A": "src-r05-a", "B": "src-r05-b"}
EVIDENCE_IDS = {
    "A": ["ev-r05-a-1", "ev-r05-a-2", "ev-r05-a-3"],
    "B": ["ev-r05-b-1", "ev-r05-b-2", "ev-r05-b-3"],
}

OBJECT_NAMES = {"A": "FIC-A-1", "B": "FIC-B-1"}


def roster_record_id(paper: PaperId, execution_id: str) -> str:
    """提取输出（名单/论文/对象）按执行隔离编号。

    来源包与证据是可共享的固定输入，同内容重跑按不可变修订复用；
    名单、论文贡献与对象是执行的提取输出，重跑或换配置的新执行
    各自生成新编号，互不覆盖或混用。
    """
    return f"roster-r05-{paper.lower()}-{execution_id}"


def paper_record_id(paper: PaperId, execution_id: str) -> str:
    return f"paper-r05-{paper.lower()}-{execution_id}"


def object_record_id(paper: PaperId, execution_id: str) -> str:
    return f"obj-r05-{paper.lower()}-{execution_id}"


LINES = {
    "A": [
        (
            "We selected FIC-A-1 for follow-up spectroscopy because it had previously"
            " been identified as a Galactic unbound candidate."
        ),
        (
            "Our new spectrum gives a heliocentric radial velocity of 520 +/- 10 km/s"
            " for FIC-A-1."
        ),
        (
            "Under model M1 FIC-A-1 is consistent with being bound to the Milky Way,"
            " while under model M2 it remains unbound; we defer a definitive overall"
            " conclusion."
        ),
    ],
    "B": [
        (
            "We selected FIC-B-1 for follow-up spectroscopy because it had previously"
            " been identified as a Galactic unbound candidate."
        ),
        (
            "Our new spectrum gives a heliocentric radial velocity of 480 +/- 15 km/s"
            " for FIC-B-1."
        ),
        "We conclude that FIC-B-1 is unbound to the Milky Way.",
    ],
}

#: 固定的测试决定：A 的作者判断歧义由专家读为“无总体结论”。
EXPECTED_STATUS = {"A": "no_overall_conclusion", "B": "unbound"}


def paper_key(paper: PaperId) -> str:
    return PAPER_KEYS[paper]


def tex_bytes(paper: PaperId) -> bytes:
    return ("\n".join(LINES[paper]) + "\n").encode("utf-8")


def source_relative_path(paper: PaperId) -> str:
    return f"sources/r05/{paper.lower()}/paper.tex"


def evidence_excerpt(paper: PaperId, index: int) -> str:
    return LINES[paper][index]


def needs_expert(paper: PaperId) -> bool:
    return paper == "A"


def build_source_and_evidence(
    paper: PaperId, *, created_at: datetime, execution_id: str | None
):
    """构造来源包与三条证据记录；文件须已按内容发布到空间。"""
    from ..records.source import (
        EvidenceData,
        EvidenceRecord,
        SourceFile,
        SourcePackageData,
        SourcePackageRecord,
    )

    source = build_record(
        SourcePackageRecord,
        record_id=SOURCE_IDS[paper],
        created_at=created_at,
        execution_id=execution_id,
        data=SourcePackageData(
            paper_key=PAPER_KEYS[paper],
            origin={"kind": "synthetic"},
            source_version="fictional-v1",
            files=[
                SourceFile(
                    file_id="paper",
                    path=source_relative_path(paper),
                    sha256=_sha256(tex_bytes(paper)),
                    media_type="text/x-tex",
                    role="main_text",
                    read_state="ready",
                    derived_from=[],
                )
            ],
            gaps=[],
        ),
    )
    evidences = [
        build_record(
            EvidenceRecord,
            record_id=EVIDENCE_IDS[paper][index],
            created_at=created_at,
            execution_id=execution_id,
            data=EvidenceData(
                paper_key=PAPER_KEYS[paper],
                source_ref=source.ref().model_dump(mode="json"),
                file_id="paper",
                locator={
                    "kind": "text",
                    "start_line": index + 1,
                    "end_line": index + 1,
                },
                excerpt=evidence_excerpt(paper, index),
            ),
        )
        for index in range(3)
    ]
    return source, evidences


def roster_record(
    paper: PaperId,
    *,
    source_ref: RecordRef,
    created_at: datetime,
    execution_id: str | None,
):
    from ..records.roster import RosterData, RosterRecord

    entry_item = f"roster-{paper}-1"
    contribution_item = f"contribution-{paper}-1"
    evidence_ref = RecordRef(record_id=EVIDENCE_IDS[paper][0], revision=1).model_dump(
        mode="json"
    )
    return build_record(
        RosterRecord,
        record_id=roster_record_id(paper, execution_id or ""),
        created_at=created_at,
        execution_id=execution_id,
        data=RosterData(
            paper_key=PAPER_KEYS[paper],
            source_ref=source_ref.model_dump(mode="json"),
            method="direct",
            coverage={
                "state": "complete",
                "checked_file_ids": ["paper"],
                "gap_refs": [],
            },
            entries=[
                {
                    "item_id": entry_item,
                    "identifiers": [
                        {
                            "value": OBJECT_NAMES[paper],
                            "evidence_refs": [evidence_ref],
                        }
                    ],
                    "disposition": "included",
                    "evidence_refs": [evidence_ref],
                    "issue_refs": [],
                    "contribution_id": contribution_item,
                }
            ],
            unresolved_groups=[],
            issue_refs=[],
        ),
    )


def paper_record(
    paper: PaperId,
    *,
    source_ref: RecordRef,
    roster_ref: RecordRef,
    question_ref: RecordRef | None,
    created_at: datetime,
    execution_id: str | None,
):
    """构造论文记录；存在歧义时作者判断关联未决问题。"""
    from ..records.paper import MeasuredValue, ObjectContribution, PaperRecord
    from ..records.rules import quantity_paths

    evidence = [
        RecordRef(record_id=EVIDENCE_IDS[paper][i], revision=1) for i in range(3)
    ]
    ev = [ref.model_dump(mode="json") for ref in evidence]
    boundness_payload = {
        "status": None,
        "evidence_refs": [],
        "issue_refs": [question_ref.model_dump(mode="json")]
        if question_ref is not None
        else [],
        "gap_refs": [],
    }
    if not needs_expert(paper):
        boundness_payload = {
            "status": EXPECTED_STATUS[paper],
            "evidence_refs": [ev[2]],
            "issue_refs": [],
            "gap_refs": [],
        }
    number = "520" if paper == "A" else "480"
    error = "10" if paper == "A" else "15"
    value = MeasuredValue.model_validate(
        {
            "value_id": f"rv-{paper.lower()}-1",
            "value": number,
            "error": error,
            "lower_error": None,
            "upper_error": None,
            "unit": "km/s",
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
            "condition": "New spectrum; heliocentric frame.",
            "frame": "heliocentric",
            "epoch": None,
            "coordinate_format": None,
            "representation": None,
            "purpose": "measurement",
            "source": "this_paper",
            "source_note": None,
            "author_usage": None,
            "direct_evidence": [
                {"part": "value", "evidence_ref": ev[1], "raw_fragment": number},
                {"part": "error", "evidence_ref": ev[1], "raw_fragment": error},
            ],
            "context_evidence_refs": [],
        }
    )
    conditional = []
    if needs_expert(paper):
        conditional = [
            {
                "condition": "Under model M1.",
                "statement": "Consistent with being bound to the Milky Way.",
                "evidence_refs": [ev[2]],
            },
            {
                "condition": "Under model M2.",
                "statement": "Remains unbound.",
                "evidence_refs": [ev[2]],
            },
        ]
    contribution = ObjectContribution.model_validate(
        {
            "item_id": f"contribution-{paper}-1",
            "roster_item_id": f"roster-{paper}-1",
            "identifiers": [{"value": OBJECT_NAMES[paper], "evidence_refs": [ev[0]]}],
            "contribution_summary": {
                "text": (
                    "Follow-up spectroscopy provides a new heliocentric radial"
                    " velocity and reassesses the object's Galactic boundness."
                ),
                "evidence_refs": [ev[0], ev[1]],
            },
            "contribution_type": {"value": "follow_up", "evidence_refs": [ev[0]]},
            "paper_boundness": boundness_payload,
            "conditional_assessments": conditional,
            "quantities": [
                {
                    "quantity": "observed_phase_space.radial_velocity",
                    "values": [value.model_dump(mode="json")],
                }
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
    return build_record(
        PaperRecord,
        record_id=paper_record_id(paper, execution_id or ""),
        created_at=created_at,
        execution_id=execution_id,
        data={
            "paper_key": PAPER_KEYS[paper],
            "metadata": {
                "arxiv_id": None,
                "version": "fictional-v1",
                "title": None,
                "authors": [],
            },
            "source_ref": source_ref.model_dump(mode="json"),
            "roster_ref": roster_ref.model_dump(mode="json"),
            "object_contributions": [contribution.model_dump(mode="json")],
        },
    )


def question_content(
    paper: PaperId,
    *,
    source_ref: RecordRef,
    rules: RulesRef,
    paper_id: str,
):
    """A 篇作者判断歧义的问题内容；目标指向论文贡献条目。"""
    from ..review.service import QuestionContent

    return QuestionContent(
        category="author_assessment",
        target_refs=[
            ItemRef(
                record_id=paper_id,
                revision=1,
                item_id=f"contribution-{paper}-1",
            )
        ],
        source_refs=[source_ref],
        rules_ref=rules,
        prompt=(
            "The author gives opposite conditional conclusions under models M1"
            " and M2 without an overall assessment. Determine the author's"
            " overall boundness assessment status for this contribution."
        ),
        evidence_refs=[RecordRef(record_id=EVIDENCE_IDS[paper][2], revision=1)],
    )


def expert_decision_patch(target: ItemRef, *, evidence_ref: RecordRef):
    """固定的测试决定补丁：把作者总体判断定为无总体结论。

    目标条目与证据引用来自问题本身（``question_content`` 的目标与
    证据），不按论文编号另行推断。
    """
    from ..records.questions import DecisionPatch, PatchChange

    return DecisionPatch(
        target=target,
        changes=[
            PatchChange(
                field="paper_boundness",
                value={
                    "status": "no_overall_conclusion",
                    "evidence_refs": [evidence_ref.model_dump(mode="json")],
                    "issue_refs": [],
                    "gap_refs": [],
                },
            )
        ],
    )


def object_record(
    paper: PaperId,
    *,
    paper_ref: RecordRef,
    created_at: datetime,
    execution_id: str | None,
):
    from ..records.objects import ContributionLink, ObjectData, ObjectRecord

    return build_record(
        ObjectRecord,
        record_id=object_record_id(paper, execution_id or ""),
        created_at=created_at,
        execution_id=execution_id,
        data=ObjectData(
            object_id=f"fic-r05-{paper.lower()}",
            display_name=OBJECT_NAMES[paper],
            aliases=[],
            contribution_links=[
                ContributionLink(
                    target_ref=paper_ref,
                    item_id=f"contribution-{paper}-1",
                    state="confirmed",
                    basis="Single contribution links this object to one paper.",
                    evidence_refs=[
                        RecordRef(record_id=EVIDENCE_IDS[paper][0], revision=1)
                    ],
                    decision_refs=[],
                )
            ],
            external_links=[],
            changes=[],
        ),
    )


def _sha256(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()
