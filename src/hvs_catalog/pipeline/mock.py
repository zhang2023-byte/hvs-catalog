"""共享提取入口的模拟材料与服务（仅 synthetic-mock 实现）。

材料沿用既有 synthetic 合成素材（``FIC-R05-A`` 作者判断歧义、
``FIC-R05-B`` 含义明确），另增 ``FIC-CANDIDATE-C`` 覆盖候选方法的
纳入/排除/未决三种处置。服
务按固定材料快照确定性应答并记录调用与 token 用量，不产生任何货币费
用。全部标识为虚构，不是真实论文、Gold 或正式判例；真实提取不在本
里程碑内，未支持的论文明确报错而不是退化为空结果。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..records.common import RecordRef, build_record, sha256_of
from ..records.questions import RulesRef
from ..review.service import QuestionContent
from ..synthetic import material as m
from ..synthetic.runner import import_materials as import_base_materials
from ..workspace import CommitRequest, Workspace

PAPER_A = m.PAPER_KEYS["A"]
PAPER_B = m.PAPER_KEYS["B"]
PAPER_C = "FIC-CANDIDATE-C"

SUPPORTED_PAPER_KEYS = (PAPER_A, PAPER_B, PAPER_C)

#: 既有 synthetic 合成素材的论文标识字母（A/B 两篇历史合成论文）。
BASE_LETTERS = {PAPER_A: "A", PAPER_B: "B"}

#: 模拟服务的固定 token 用量（模拟调用“知道”自己的用量；不涉及费用）。
STEP_TOKENS = {
    "direct_roster": 120,
    "discover_candidates": 80,
    "review_candidates": 90,
    "quantities": 200,
    "interpret_ambiguity": 60,
}

_MODEL_ATTEMPT_NOTE = (
    "Model attempted to interpret the ambiguity against the paper evidence"
    " and both model contexts; it could not determine the author's intent."
    " The question remains unresolved."
)

C_LINES = [
    (
        "We examined three candidate high-velocity objects from the survey"
        " cross-match table."
    ),
    (
        "FIC-C-1 is confirmed as an unbound candidate with a heliocentric"
        " radial velocity of 430 +/- 12 km/s."
    ),
    ("FIC-C-2 is a thin-disk Galactic star and is excluded from the candidate sample."),
    (
        "The status of FIC-C-3 remains unclear because its proper-motion"
        " solution is ambiguous; we defer a decision on its inclusion."
    ),
    (
        "A provisional heliocentric radial velocity of 405 +/- 9 km/s is"
        " reported for FIC-C-3."
    ),
]

C_SOURCE_ID = "src-candidate-c"
C_EVIDENCE_IDS = [f"ev-candidate-c-{index}" for index in range(len(C_LINES))]

_QUANTITY_RV = "observed_phase_space.radial_velocity"


def _slug(value: str) -> str:
    return value.lower().replace("_", "-")


def roster_record_id(paper_key: str, execution_id: str) -> str:
    """提取输出按执行隔离编号；来源与证据是可共享的固定输入。"""
    return f"roster-{_slug(paper_key)}-{execution_id}"


def paper_record_id(paper_key: str, execution_id: str) -> str:
    return f"paper-{_slug(paper_key)}-{execution_id}"


def object_record_id(paper_key: str, identifier: str, execution_id: str) -> str:
    return f"obj-{_slug(paper_key)}-{_slug(identifier)}-{execution_id}"


def c_tex_bytes() -> bytes:
    return ("\n".join(C_LINES) + "\n").encode("utf-8")


def c_source_relative_path() -> str:
    return "sources/candidate/c/paper.tex"


def _boundness_fixed(status: str, line: int) -> dict:
    return {"kind": "fixed", "status": status, "evidence_line": line}


def _boundness_expert(conditionals: list[dict]) -> dict:
    return {"kind": "needs_expert", "conditionals": conditionals}


def _rv_value(value_id: str, value: str, error: str, line: int, condition: str) -> dict:
    return {
        "quantity": _QUANTITY_RV,
        "value_id": value_id,
        "value": value,
        "error": error,
        "unit": "km/s",
        "evidence_line": line,
        "condition": condition,
        "frame": "heliocentric",
    }


_MOCK_PAPERS: dict[str, dict[str, Any]] = {
    PAPER_A: {
        "paper_key": PAPER_A,
        "source_id": m.SOURCE_IDS["A"],
        "evidence_ids": list(m.EVIDENCE_IDS["A"]),
        "roster_entries": [
            {
                "item_id": "roster-a-1",
                "identifier": m.OBJECT_NAMES["A"],
                "disposition": "included",
                "evidence_line": 0,
                "contribution_id": "contribution-a-1",
            }
        ],
        "candidates": [
            {
                "candidate_id": "cand-a-1",
                "identifier": m.OBJECT_NAMES["A"],
                "item_id": "roster-a-1",
                "disposition": "included",
                "evidence_line": 0,
                "contribution_id": "contribution-a-1",
            }
        ],
        "contributions": [
            {
                "contribution_id": "contribution-a-1",
                "roster_item_id": "roster-a-1",
                "identifier": m.OBJECT_NAMES["A"],
                "summary": (
                    "Follow-up spectroscopy provides a new heliocentric radial"
                    " velocity and reassesses the object's Galactic boundness."
                ),
                "summary_lines": [0, 1],
                "contribution_type": "follow_up",
                "type_evidence_line": 0,
                "identifier_evidence_line": 0,
                "boundness": _boundness_expert(
                    [
                        {
                            "condition": "Under model M1.",
                            "statement": (
                                "Consistent with being bound to the Milky Way."
                            ),
                            "evidence_line": 2,
                        },
                        {
                            "condition": "Under model M2.",
                            "statement": "Remains unbound.",
                            "evidence_line": 2,
                        },
                    ]
                ),
                "values": [
                    _rv_value(
                        "rv-a-1",
                        "520",
                        "10",
                        1,
                        "New spectrum; heliocentric frame.",
                    )
                ],
            }
        ],
    },
    PAPER_B: {
        "paper_key": PAPER_B,
        "source_id": m.SOURCE_IDS["B"],
        "evidence_ids": list(m.EVIDENCE_IDS["B"]),
        "roster_entries": [
            {
                "item_id": "roster-b-1",
                "identifier": m.OBJECT_NAMES["B"],
                "disposition": "included",
                "evidence_line": 0,
                "contribution_id": "contribution-b-1",
            }
        ],
        "candidates": [
            {
                "candidate_id": "cand-b-1",
                "identifier": m.OBJECT_NAMES["B"],
                "item_id": "roster-b-1",
                "disposition": "included",
                "evidence_line": 0,
                "contribution_id": "contribution-b-1",
            }
        ],
        "contributions": [
            {
                "contribution_id": "contribution-b-1",
                "roster_item_id": "roster-b-1",
                "identifier": m.OBJECT_NAMES["B"],
                "summary": (
                    "Follow-up spectroscopy provides a new heliocentric radial"
                    " velocity and reassesses the object's Galactic boundness."
                ),
                "summary_lines": [0, 1],
                "contribution_type": "follow_up",
                "type_evidence_line": 0,
                "identifier_evidence_line": 0,
                "boundness": _boundness_fixed("unbound", 2),
                "values": [
                    _rv_value(
                        "rv-b-1",
                        "480",
                        "15",
                        1,
                        "New spectrum; heliocentric frame.",
                    )
                ],
            }
        ],
    },
    PAPER_C: {
        "paper_key": PAPER_C,
        "source_id": C_SOURCE_ID,
        "evidence_ids": list(C_EVIDENCE_IDS),
        "roster_entries": [
            {
                "item_id": "cand-c-1",
                "identifier": "FIC-C-1",
                "disposition": "included",
                "evidence_line": 1,
                "contribution_id": "contribution-c-1",
            },
            {
                "item_id": "cand-c-2",
                "identifier": "FIC-C-2",
                "disposition": "excluded",
                "evidence_line": 2,
                "contribution_id": None,
            },
            {
                "item_id": "cand-c-3",
                "identifier": "FIC-C-3",
                "disposition": "pending",
                "evidence_line": 3,
                "contribution_id": "contribution-c-3",
            },
        ],
        "candidates": [
            {
                "candidate_id": "cand-c-1",
                "identifier": "FIC-C-1",
                "item_id": "cand-c-1",
                "disposition": "included",
                "evidence_line": 1,
                "contribution_id": "contribution-c-1",
            },
            {
                "candidate_id": "cand-c-2",
                "identifier": "FIC-C-2",
                "item_id": "cand-c-2",
                "disposition": "excluded",
                "evidence_line": 2,
                "contribution_id": None,
            },
            {
                "candidate_id": "cand-c-3",
                "identifier": "FIC-C-3",
                "item_id": "cand-c-3",
                "disposition": "pending",
                "evidence_line": 3,
                "contribution_id": "contribution-c-3",
            },
        ],
        "contributions": [
            {
                "contribution_id": "contribution-c-1",
                "roster_item_id": "cand-c-1",
                "identifier": "FIC-C-1",
                "summary": (
                    "The paper confirms FIC-C-1 as an unbound candidate with a"
                    " new heliocentric radial velocity."
                ),
                "summary_lines": [0, 1],
                "contribution_type": "candidates_found",
                "type_evidence_line": 0,
                "identifier_evidence_line": 1,
                "boundness": _boundness_fixed("unbound", 1),
                "values": [
                    _rv_value(
                        "rv-c-1",
                        "430",
                        "12",
                        1,
                        "Survey cross-match; heliocentric frame.",
                    )
                ],
            },
            {
                "contribution_id": "contribution-c-3",
                "roster_item_id": "cand-c-3",
                "identifier": "FIC-C-3",
                "summary": (
                    "The paper reports a provisional radial velocity for"
                    " FIC-C-3 and defers an overall assessment."
                ),
                "summary_lines": [0, 3],
                "contribution_type": "candidates_found",
                "type_evidence_line": 0,
                "identifier_evidence_line": 3,
                "boundness": _boundness_fixed("no_overall_conclusion", 3),
                "values": [
                    _rv_value(
                        "rv-c-3",
                        "405",
                        "9",
                        4,
                        "Provisional solution; heliocentric frame.",
                    )
                ],
            },
        ],
    },
}


def material_spec(paper_key: str) -> dict:
    """返回固定材料规格；未知论文明确报错，不退化为空结果。"""
    try:
        return _MOCK_PAPERS[paper_key]
    except KeyError:
        raise ValueError(
            f"论文 {paper_key!r} 不在模拟材料支持范围（当前支持："
            + ", ".join(SUPPORTED_PAPER_KEYS)
            + "）；真实提取不在本里程碑内"
        ) from None


class MaterialBindingError(ValueError):
    """来源记录与可信登记的模拟材料绑定不一致。

    成员资格（paper_key 在支持集内）不等于实际材料绑定：来源标识、
    受支持修订、来源版本、origin 与材料文件清单/角色/指纹必须与可信
    mock 定义全部一致。未登记的版本或内容不能使用静态应答，也不能
    改指其他来源或修订。
    """


def registered_source_binding(paper_key: str) -> dict:
    """从可信 mock 定义派生论文的登记来源绑定（相对路径，不依赖空间位置）。"""

    def _sha(data: bytes) -> str:
        import hashlib

        return hashlib.sha256(data).hexdigest()

    spec = material_spec(paper_key)
    letter = BASE_LETTERS.get(paper_key)
    if letter is not None:
        path = m.source_relative_path(letter)
        sha256 = _sha(m.tex_bytes(letter))
    else:
        path = c_source_relative_path()
        sha256 = _sha(c_tex_bytes())
    return {
        "source_id": spec["source_id"],
        "source_revision": 1,
        "source_version": "fictional-v1",
        "origin_kind": "synthetic",
        "files": (
            {
                "file_id": "paper",
                "path": path,
                "sha256": sha256,
                "media_type": "text/x-tex",
                "role": "main_text",
                "read_state": "ready",
                "derived_from": [],
            },
        ),
        "gaps": (),
    }


def resolve_registered_material(record) -> dict:
    """集中核验来源记录与登记模拟材料的绑定，返回材料规格。

    入口运行前检查、恢复与流程解析共用本入口：paper_key 成员资格、
    来源标识、受支持修订、来源版本/origin、材料文件清单/角色/指纹与
    影响科学可用性的元数据（文件读取状态 read_state、派生链
    derived_from、材料缺口 source.gaps）联合核对，任一不一致即在任何
    服务调用前拒绝；时间戳与执行写入簿记（created_at/execution_id）
    与科学输入无关，不做机械相等比较。文件实际内容与指纹的相符由
    既有 runtime 完整性核验保证，这里不重复读文件。
    """
    from ..records.source import SourcePackageRecord

    if not isinstance(record, SourcePackageRecord):
        raise MaterialBindingError(
            f"记录不是来源包：{record.record_id} v{record.revision}"
            f"（{record.RECORD_NAME}）"
        )
    paper_key = record.data.paper_key
    if paper_key not in _MOCK_PAPERS:
        raise MaterialBindingError(
            f"论文 {paper_key!r} 不在模拟材料支持范围（当前支持："
            + ", ".join(SUPPORTED_PAPER_KEYS)
            + "）；真实提取不在本里程碑内"
        )
    binding = registered_source_binding(paper_key)
    problems: list[str] = []
    if record.record_id != binding["source_id"]:
        problems.append(
            f"来源标识 {record.record_id!r} 不是论文 {paper_key} 登记的"
            f" {binding['source_id']!r}"
        )
    if record.revision != binding["source_revision"]:
        problems.append(
            f"来源修订 v{record.revision} 未在模拟材料登记"
            f"（受支持：v{binding['source_revision']}）；未登记的版本或内容"
            "不能使用静态应答，也不能改指其他来源或修订"
        )
    if record.data.source_version != binding["source_version"]:
        problems.append(
            f"来源版本 {record.data.source_version!r} 与登记的"
            f" {binding['source_version']!r} 不一致"
        )
    if record.data.origin.kind != binding["origin_kind"]:
        problems.append("来源 origin 与登记的合成来源不一致")
    files = record.data.files
    expected_files = binding["files"]
    if len(files) != len(expected_files):
        problems.append(
            f"材料文件清单与登记不一致：{len(files)} 个文件，"
            f"登记 {len(expected_files)} 个"
        )
    else:
        for actual, expected in zip(files, expected_files):
            for field in (
                "file_id",
                "path",
                "sha256",
                "media_type",
                "role",
                "read_state",
                "derived_from",
            ):
                if getattr(actual, field) != expected[field]:
                    problems.append(
                        f"材料文件 {actual.file_id!r} 的 {field} 与登记不一致"
                        f"（实际 {getattr(actual, field)!r}，登记"
                        f" {expected[field]!r}）；未登记的读取状态或派生关系"
                        "不能使用静态应答，也不能把不可读当可读"
                    )
    gaps = record.data.gaps
    expected_gaps = binding["gaps"]
    if len(gaps) != len(expected_gaps):
        problems.append(
            f"材料缺口与登记不一致：{len(gaps)} 个缺口"
            f"（{[g.gap_id for g in gaps]}），登记的 v1 材料没有缺口；"
            "未登记的缺口不能当作完整可用材料使用"
        )
    else:
        for actual, expected in zip(gaps, expected_gaps):
            if actual.model_dump(mode="json") != expected:
                problems.append(f"材料缺口 {actual.gap_id!r} 与登记不一致")
    if problems:
        raise MaterialBindingError(
            "来源记录与登记模拟材料的绑定不一致：" + "；".join(problems)
        )
    return material_spec(paper_key)


def evidence_ref(spec: dict, line: int) -> RecordRef:
    return RecordRef(record_id=spec["evidence_ids"][line], revision=1)


def source_ref(spec: dict) -> RecordRef:
    return RecordRef(record_id=spec["source_id"], revision=1)


def import_mock_source(space: Workspace, paper_key: str) -> RecordRef:
    """发布合成论文文件并提交来源包与证据记录（显式本地清单导入）。

    A/B 沿用既有 synthetic 合成素材的导入入口，同内容按不可变修订复用。
    """
    letter = BASE_LETTERS.get(paper_key)
    if letter is not None:
        return import_base_materials(space, letter)
    if paper_key != PAPER_C:
        raise ValueError(f"论文 {paper_key!r} 不在模拟材料支持范围")
    ref = space.publish_source_file("candidate/c/paper.tex", c_tex_bytes())
    assert ref.path == c_source_relative_path()
    from ..records.source import (
        EvidenceData,
        EvidenceRecord,
        SourceFile,
        SourcePackageData,
        SourcePackageRecord,
    )

    created_at = datetime.now(UTC)
    source = build_record(
        SourcePackageRecord,
        record_id=C_SOURCE_ID,
        created_at=created_at,
        execution_id=None,
        data=SourcePackageData(
            paper_key=PAPER_C,
            origin={"kind": "synthetic"},
            source_version="fictional-v1",
            files=[
                SourceFile(
                    file_id="paper",
                    path=c_source_relative_path(),
                    sha256=ref.sha256,
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
            record_id=C_EVIDENCE_IDS[line],
            created_at=created_at,
            execution_id=None,
            data=EvidenceData(
                paper_key=PAPER_C,
                source_ref=source.ref().model_dump(mode="json"),
                file_id="paper",
                locator={"kind": "text", "start_line": line + 1, "end_line": line + 1},
                excerpt=C_LINES[line],
            ),
        )
        for line in range(len(C_LINES))
    ]
    space.store().commit(
        CommitRequest(
            operation_key=f"import:{PAPER_C}",
            kind="material_import",
            new_records=[source, *evidences],
            result_refs=[source.ref()],
        )
    )
    return source.ref()


# ---- 问题内容与固定测试决定 ----


def author_assessment_question(
    paper_key: str, *, paper_id: str, source: RecordRef, rules: RulesRef
) -> QuestionContent:
    """A 篇作者判断歧义的问题内容；目标指向论文贡献条目。"""
    if paper_key != PAPER_A:  # pragma: no cover - 模拟材料只有 A 有此歧义
        raise ValueError(f"{paper_key} 没有作者判断歧义材料")
    from ..records.common import ItemRef

    spec = material_spec(paper_key)
    return QuestionContent(
        category="author_assessment",
        target_refs=[
            ItemRef(
                record_id=paper_id,
                revision=1,
                item_id="contribution-a-1",
            )
        ],
        source_refs=[source],
        rules_ref=rules,
        prompt=(
            "The author gives opposite conditional conclusions under models M1"
            " and M2 without an overall assessment. Determine the author's"
            " overall boundness assessment status for this contribution."
        ),
        evidence_refs=[evidence_ref(spec, 2)],
    )


def inclusion_question(
    paper_key: str,
    *,
    roster_id: str,
    item_id: str,
    source: RecordRef,
    rules: RulesRef,
) -> QuestionContent:
    """C 篇候选处置歧义的问题内容；目标指向名单条目。"""
    spec = material_spec(paper_key)
    return QuestionContent(
        category="inclusion",
        target_refs=[_item_ref(spec, roster_id=roster_id, item_id=item_id)],
        source_refs=[source],
        rules_ref=rules,
        prompt=(
            "The paper states that the status of FIC-C-3 remains unclear and"
            " defers a decision on its inclusion. Determine whether FIC-C-3"
            " should be included in, excluded from, or kept pending in the"
            " roster."
        ),
        evidence_refs=[evidence_ref(spec, 3)],
    )


def _item_ref(spec: dict, *, roster_id: str, item_id: str):
    from ..records.common import ItemRef

    return ItemRef(record_id=roster_id, revision=1, item_id=item_id)


def expert_patch_for(question_category: str, *, target, evidence: RecordRef):
    """固定测试决定补丁：按问题类别选择补丁内容。

    作者判断歧义按既有 synthetic 固定内容读为“无总体结论”；候选处置可把未决
    条目改为收录（contribution_id 与证据随补丁给出）。
    """
    from ..records.questions import DecisionPatch, PatchChange

    if question_category == "author_assessment":
        return m.expert_decision_patch(target, evidence_ref=evidence)
    if question_category == "inclusion":
        return DecisionPatch(
            target=target,
            changes=[
                PatchChange(field="disposition", value="included"),
                PatchChange(field="contribution_id", value="contribution-c-3"),
                PatchChange(
                    field="evidence_refs", value=[evidence.model_dump(mode="json")]
                ),
                PatchChange(field="issue_refs", value=[]),
            ],
        )
    raise ValueError(f"未知问题类别：{question_category!r}")  # pragma: no cover


PIPELINE_EXPERT_ID = "pipeline-synthetic-expert-01"


def submit_expert_decision(
    space: Workspace,
    *,
    execution_id: str,
    question_ref: RecordRef,
    request_key: str,
    choice: str,
):
    """按固定测试内容提交模拟人类专家决定。

    ``choice`` 只接受固定值：``no_overall_conclusion``（作者判断歧义，
    resolved）、``include``（候选处置，resolved）与 ``keep_pending``
    （unresolved，不携带补丁）。决定内容来自模拟材料，不从 Gold 或
    评分读取。
    """
    from ..records.questions import Actor
    from ..review import DecisionSubmission, ReviewService

    review = ReviewService(space)
    view = review.get_question(question_ref)
    category = view.record.data.category
    answers = {
        "author_assessment": {
            "answer_original": "无总体结论：作者在两种模型下给出相反结论且未作总体判断。",
            "answer_en": (
                "No overall conclusion: the author gives opposite conditional"
                " conclusions and defers an overall assessment."
            ),
        },
        "inclusion": {
            "answer_original": "收录 FIC-C-3：作者明确报告其测量值，可进入名单。",
            "answer_en": (
                "Include FIC-C-3: the paper reports its measurement and it"
                " belongs in the roster."
            ),
        },
    }
    if category not in answers:
        raise ValueError(f"模拟决定不支持问题类别 {category!r}")
    answer = answers[category]
    outcome = "unresolved" if choice == "keep_pending" else "resolved"
    patch = None
    if outcome == "resolved":
        if choice == "no_overall_conclusion" and category != "author_assessment":
            raise ValueError("no_overall_conclusion 只适用于作者判断歧义问题")
        if choice == "include" and category != "inclusion":
            raise ValueError("include 只适用于候选处置问题")
        target = view.record.data.target_refs[0]
        patch = expert_patch_for(
            category, target=target, evidence=view.record.data.evidence_refs[0]
        )
    elif choice != "keep_pending":
        raise ValueError(f"未知模拟决定：{choice!r}")
    submission = DecisionSubmission(
        question_ref=question_ref,
        fingerprint=view.record.data.fingerprint,
        actor=Actor(kind="human", id=PIPELINE_EXPERT_ID),
        outcome=outcome,
        answer_original=answer["answer_original"],
        answer_en=answer["answer_en"],
        reason="Synthetic expert decision fixed in the pipeline mock materials.",
        evidence_refs=[],
        patch=patch,
    )
    return review.submit_decision(
        execution_id=execution_id,
        step_id=view.step_id or "",
        submission=submission,
        request_key=request_key,
    )


# ---- 模拟服务 ----


def _respond(step: str, request: dict, material: dict) -> dict:
    spec = material_spec(material["paper_key"])
    tokens = STEP_TOKENS.get(step)
    if step == "direct_roster":
        return {"entries": _entry_responses(spec["roster_entries"]), "tokens": tokens}
    if step == "discover_candidates":
        return {"candidates": _entry_responses(spec["candidates"]), "tokens": tokens}
    if step == "review_candidates":
        candidate_id = request["candidate_id"]
        entry = next(
            (
                item
                for item in spec["candidates"]
                if item["candidate_id"] == candidate_id
            ),
            None,
        )
        if entry is None:
            raise ValueError(f"未知候选：{candidate_id}")
        return {"entry": _entry_response(entry), "tokens": tokens}
    if step == "quantities":
        included = set(request["included_items"])
        contributions = [
            item for item in spec["contributions"] if item["roster_item_id"] in included
        ]
        needs_expert = any(
            item["boundness"]["kind"] == "needs_expert" for item in contributions
        )
        return {
            "contributions": contributions,
            "needs_expert": needs_expert,
            "tokens": tokens,
        }
    if step == "interpret_ambiguity":
        return {"attempt": "unresolved", "note": _MODEL_ATTEMPT_NOTE, "tokens": tokens}
    raise ValueError(f"未知的服务步骤：{step}")


def _entry_response(entry: dict) -> dict:
    payload = {
        "item_id": entry["item_id"],
        "identifier": entry["identifier"],
        "disposition": entry["disposition"],
        "evidence": {"file_id": "paper", "line": entry["evidence_line"]},
    }
    if entry.get("candidate_id"):
        payload["candidate_id"] = entry["candidate_id"]
    if entry.get("contribution_id") and entry["disposition"] == "included":
        payload["contribution_id"] = entry["contribution_id"]
    return payload


def _entry_responses(entries: list[dict]) -> list[dict]:
    return [_entry_response(entry) for entry in entries]


class PipelineMockService:
    """按固定材料快照确定性应答的模拟模型服务。

    调用记录与 token 用量保存在进程内日志中，供测试与报告核对；可选
    的 ``journal_path`` 把每次调用追加写入 JSON 行文件，供跨进程测试
    与集成报告核对实际调用次数。不计算或保存任何货币费用。
    """

    def __init__(self, journal_path: str | Path | None = None) -> None:
        self.calls: list[dict] = []
        self._counter = 0
        self.journal_path = Path(journal_path) if journal_path is not None else None
        if self.journal_path is not None:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)

    def call(self, *, step: str, request: dict, material: dict) -> dict:
        self._counter += 1
        response = _respond(step, request, material)
        entry = {
            "step": step,
            "request_digest": sha256_of(request),
            "service_request_id": f"pipeline-mock-{self._counter}",
            "tokens": response.get("tokens"),
            "model": (request.get("model") or {}).get("model"),
        }
        self.calls.append(entry)
        if self.journal_path is not None:
            with self.journal_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return response

    def calls_for_step(self, step: str) -> int:
        return sum(1 for call in self.calls if call["step"] == step)

    def summary(self) -> dict:
        by_step: dict[str, int] = {}
        for call in self.calls:
            by_step[call["step"]] = by_step.get(call["step"], 0) + 1
        tokens = [call["tokens"] for call in self.calls if call["tokens"] is not None]
        return {
            "calls": len(self.calls),
            "steps": by_step,
            "tokens": sum(tokens) if len(tokens) == len(self.calls) else None,
        }


def mock_preflight(space: Workspace, request) -> list[str]:
    """运行前检查：模拟实现的支持范围与来源登记绑定。

    与流程/恢复共用 ``resolve_registered_material`` 同一核验入口，
    避免不同路径采用不同材料。
    """
    problems: list[str] = []
    if len(request.source_refs) != 1:
        problems.append(
            f"模拟实现目前只支持单一来源包，请求包含 {len(request.source_refs)} 个"
        )
    else:
        ref = request.source_refs[0]
        record = space.store().read_record(ref)
        if record is None:
            problems.append(f"来源记录缺失：{ref.record_id} v{ref.revision}")
        else:
            try:
                resolve_registered_material(record)
            except MaterialBindingError as exc:
                problems.append(str(exc))
    for ref in request.existing_decision_refs:
        if space.store().read_record(ref) is None:
            problems.append(f"固定允许的决定缺失：{ref.record_id} v{ref.revision}")
    return problems


def material_snapshot_file(path: str | Path, paper_key: str) -> None:
    """把固定材料规格写为规范 JSON（测试夹具与诊断用）。"""
    Path(path).write_text(
        json.dumps(
            material_spec(paper_key), ensure_ascii=False, sort_keys=True, indent=2
        ),
        encoding="utf-8",
    )
