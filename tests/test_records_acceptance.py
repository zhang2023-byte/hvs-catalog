"""验收反例回归：引用、数值一致性、缺口、冲突、指纹、补丁、发布与定位。

对应 .work/archive/r04-acceptance/review.md 的 9 项修复要求；每个场景都基于正式合成
夹具构造，同时覆盖填写转换与保存记录直接加载两条入口。全部材料为虚构。
"""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError
from records_helpers import FIXTURES, load_fixture, refresh_release_manifest
from test_submission_conversion import (
    EV_520,
    gap_conflict_submission,
    make_context,
    roster_context,
)

from hvs_catalog.records import (
    CollectionValidationError,
    convert_quantity_submission,
    convert_roster_submission,
    parse_record,
    validate_collection,
)
from hvs_catalog.records.runs import compute_release_manifest_sha256
from hvs_catalog.records.submission import QuantitySubmission, RosterSubmission


def payloads(name: str = "numeric-forms") -> list[dict]:
    return [
        record.model_dump(mode="json", by_alias=True) for record in load_fixture(name)
    ]


def find(rows: list[dict], record_id: str) -> dict:
    return next(row for row in rows if row["record_id"] == record_id)


def check(rows: list[dict]):
    return validate_collection(
        [parse_record(row) for row in rows], materials_root=FIXTURES
    )


def check_rejected(rows: list[dict], *fragments: str) -> list[str]:
    with pytest.raises(CollectionValidationError) as excinfo:
        check(rows)
    messages = [str(issue) for issue in excinfo.value.issues]
    for fragment in fragments:
        assert any(fragment in message for message in messages), (
            f"期望包含 {fragment!r} 的问题，实际：{messages}"
        )
    return messages


# ---------------------------------------------------------------------------
# 修复 1：科学证据引用的类型、论文归属与准确修订
# ---------------------------------------------------------------------------


def test_summary_evidence_must_reference_evidence_record():
    rows = payloads()
    source = find(rows, "numeric-forms-source")
    paper = find(rows, "numeric-forms-paper")
    paper["data"]["object_contributions"][0]["contribution_summary"][
        "evidence_refs"
    ] = [{"record_id": source["record_id"], "revision": 1}]
    check_rejected(rows, "应指向 evidence 记录")


def test_numeric_evidence_must_belong_to_same_paper():
    rows = payloads()
    source = copy.deepcopy(find(rows, "numeric-forms-source"))
    source["record_id"] = "foreign-source"
    source["data"]["paper_key"] = "FOREIGN-PAPER"
    rows.append(source)
    evidence = copy.deepcopy(find(rows, "numeric-forms-ev-values"))
    evidence["record_id"] = "foreign-evidence"
    evidence["data"]["paper_key"] = "FOREIGN-PAPER"
    evidence["data"]["source_ref"] = {"record_id": "foreign-source", "revision": 1}
    rows.append(evidence)
    foreign_ref = {"record_id": "foreign-evidence", "revision": 1}
    value = find(rows, "numeric-forms-paper")["data"]["object_contributions"][0][
        "quantities"
    ][0]["values"][0]
    for part in value["direct_evidence"]:
        part["evidence_ref"] = foreign_ref
    value["context_evidence_refs"] = [foreign_ref]
    # 外部证据自身的来源、原文和定位全部合法，仅论文归属错误
    check_rejected(rows, "另一篇论文的证据")


def test_fragment_checked_against_exact_evidence_revision():
    rows = payloads()
    evidence = copy.deepcopy(find(rows, "numeric-forms-ev-values"))
    evidence["revision"] = 2
    rows.append(evidence)
    value = find(rows, "numeric-forms-paper")["data"]["object_contributions"][0][
        "quantities"
    ][0]["values"][0]
    # 数值与片段保持一致，仅片段不在 v1 原文中；新增 v2 不能掩盖 v1 的问题
    value["value"] = "9999"
    value["direct_evidence"][0]["raw_fragment"] = "9999"
    check_rejected(rows, "不在证据")


# ---------------------------------------------------------------------------
# 修复 2：数值与原报片段一致
# ---------------------------------------------------------------------------


def test_saved_number_must_equal_printed_fragment():
    rows = payloads()
    value = find(rows, "numeric-forms-paper")["data"]["object_contributions"][0][
        "quantities"
    ][0]["values"][0]
    value["value"] = "999999"  # 原文和数字证据仍是 520
    with pytest.raises(ValidationError, match="与原报片段"):
        parse_record(find(rows, "numeric-forms-paper"))


def test_coverage_value_must_equal_printed_fragment():
    rows = payloads()
    value = find(rows, "numeric-forms-paper")["data"]["object_contributions"][0][
        "quantities"
    ][1]["values"][0]
    value["coverage"]["value"] = "68"
    with pytest.raises(ValidationError, match="与原报片段"):
        parse_record(find(rows, "numeric-forms-paper"))


# ---------------------------------------------------------------------------
# 修复 3：缺口影响范围与两条入口
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("empty_dimension", ["quantity", "item_id"])
def test_broad_gap_blocks_complete_declaration_in_conversion(empty_dimension):
    source = load_fixture("gap-conflict")
    source = next(r for r in source if r.RECORD_NAME == "source_package")
    payload = source.model_dump(mode="json", by_alias=True)
    payload["data"]["gaps"][0]["affected_targets"]["items"][0][empty_dimension] = None
    source = parse_record(payload)
    submission = gap_conflict_submission()
    submission["objects"][0]["coverage"]["exceptions"] = []
    with pytest.raises(ValueError, match="完成声明与已知材料缺口矛盾"):
        convert_quantity_submission(
            make_context(source), QuantitySubmission.model_validate(submission)
        )


def test_broad_gap_blocks_complete_rows_in_saved_records():
    rows = payloads("gap-conflict")
    source = find(rows, "gap-conflict-source")
    source["data"]["gaps"][0]["affected_targets"]["items"][0]["quantity"] = None
    for paper in (row for row in rows if row["schema"]["name"] == "paper"):
        for contribution in paper["data"]["object_contributions"]:
            for entry in contribution["quantity_coverage"]:
                entry["state"] = "complete"
                entry["gap_refs"] = []
    check_rejected(rows, "完成声明与已知材料缺口矛盾")


def test_saved_coverage_cannot_bypass_source_constraints():
    rows = payloads("gap-conflict")
    for paper in (row for row in rows if row["schema"]["name"] == "paper"):
        for contribution in paper["data"]["object_contributions"]:
            for entry in contribution["quantity_coverage"]:
                entry["state"] = "complete"
                entry["gap_refs"] = []
                entry["checked_file_ids"] = ["nonexistent-file"]
    check_rejected(rows, "不在来源包中", "完成声明与已知材料缺口矛盾")


def test_roster_complete_rejected_with_unknown_scope_gap():
    source = load_fixture("gap-conflict")
    source = next(r for r in source if r.RECORD_NAME == "source_package")
    payload = source.model_dump(mode="json", by_alias=True)
    payload["data"]["gaps"][0]["affected_targets"] = {"scope": "unknown", "items": []}
    source = parse_record(payload)
    submission = RosterSubmission.model_validate(
        {
            "coverage": {
                "state": "complete",
                "checked_file_ids": ["main"],
                "gap_ids": [],
            },
            "entries": [],
            "unresolved_groups": [],
        }
    )
    with pytest.raises(ValueError, match="影响范围未知的材料缺口矛盾"):
        convert_roster_submission(roster_context(source), submission)


def test_roster_unrelated_gap_reference_rejected():
    source = load_fixture("gap-conflict")
    source = next(r for r in source if r.RECORD_NAME == "source_package")
    submission = RosterSubmission.model_validate(
        {
            "coverage": {
                "state": "partial",
                "checked_file_ids": ["main"],
                "gap_ids": ["gap-velocity"],  # 已知范围缺口只影响数值提取
            },
            "entries": [],
            "unresolved_groups": [],
        }
    )
    with pytest.raises(ValueError, match="与名单无关的缺口"):
        convert_roster_submission(roster_context(source), submission)


def test_roster_not_checked_needs_no_fake_file():
    source = load_fixture("gap-conflict")
    source = next(r for r in source if r.RECORD_NAME == "source_package")
    submission = RosterSubmission.model_validate(
        {
            "coverage": {"state": "not_checked", "checked_file_ids": [], "gap_ids": []},
            "entries": [],
            "unresolved_groups": [],
        }
    )
    result = convert_roster_submission(roster_context(source), submission)
    assert result.roster.coverage.checked_file_ids == []
    assert result.roster.entries == []


def test_unknown_scope_gap_preserves_partial_results():
    source = load_fixture("gap-conflict")
    source = next(r for r in source if r.RECORD_NAME == "source_package")
    payload = source.model_dump(mode="json", by_alias=True)
    payload["data"]["gaps"][0]["affected_targets"] = {"scope": "unknown", "items": []}
    source = parse_record(payload)
    submission = gap_conflict_submission()
    coverage = submission["objects"][0]["coverage"]
    coverage["common"]["state"] = "partial"
    coverage["common"]["gap_ids"] = ["gap-velocity"]
    coverage["exceptions"] = []
    result = convert_quantity_submission(
        make_context(source), QuantitySubmission.model_validate(submission)
    )
    rows = result.objects[0].quantity_coverage
    assert all(row.state == "partial" for row in rows)
    assert all(
        any(ref.gap_id == "gap-velocity" for ref in row.gap_refs) for row in rows
    )


def _source_with_judgment_gap(judgment: str):
    source = load_fixture("gap-conflict")
    source = next(r for r in source if r.RECORD_NAME == "source_package")
    payload = source.model_dump(mode="json", by_alias=True)
    gap = payload["data"]["gaps"][0]
    gap["gap_id"] = "gap-inclusion"
    gap["description"] = (
        "Missing supplementary identification needed to decide "
        f"the {judgment} of contribution-A."
    )
    gap["affected_targets"] = {
        "scope": "known",
        "items": [
            {"item_id": "contribution-A", "quantity": None, "judgment": judgment}
        ],
    }
    return parse_record(payload)


@pytest.mark.parametrize("judgment", ["inclusion", "identity"])
def test_known_judgment_gap_rejects_complete_roster(judgment):
    source = _source_with_judgment_gap(judgment)
    submission = RosterSubmission.model_validate(
        {
            "coverage": {
                "state": "complete",
                "checked_file_ids": ["main"],
                "gap_ids": [],
            },
            "entries": [],
            "unresolved_groups": [],
        }
    )
    with pytest.raises(ValueError, match="完成声明与.*材料缺口矛盾"):
        convert_roster_submission(roster_context(source), submission)


@pytest.mark.parametrize("judgment", ["inclusion", "identity"])
def test_known_judgment_gap_partial_roster_with_gap_accepted(judgment):
    source = _source_with_judgment_gap(judgment)
    submission = RosterSubmission.model_validate(
        {
            "coverage": {
                "state": "partial",
                "checked_file_ids": ["main"],
                "gap_ids": ["gap-inclusion"],
            },
            "entries": [],
            "unresolved_groups": [],
        }
    )
    result = convert_roster_submission(roster_context(source), submission)
    assert [ref.gap_id for ref in result.roster.coverage.gap_refs] == ["gap-inclusion"]


def test_judgment_gap_does_not_block_quantity_completeness():
    source = _source_with_judgment_gap("inclusion")
    submission = gap_conflict_submission()
    submission["objects"][0]["coverage"]["exceptions"] = []
    result = convert_quantity_submission(
        make_context(source), QuantitySubmission.model_validate(submission)
    )
    # 收录判断缺口不波及物理量完成情况：18 行全部 complete，不挂判断缺口
    rows = result.objects[0].quantity_coverage
    assert len(rows) == 18 and all(row.state == "complete" for row in rows)
    assert not any(
        ref.gap_id == "gap-inclusion" for row in rows for ref in row.gap_refs
    )


def test_saved_roster_complete_rejected_with_judgment_gap():
    rows = payloads("gap-conflict")
    source = find(rows, "gap-conflict-source")
    source["data"]["gaps"][0]["affected_targets"] = {
        "scope": "known",
        "items": [
            {
                "item_id": "contribution-A",
                "quantity": None,
                "judgment": "inclusion",
            }
        ],
    }
    check_rejected(rows, "完成声明与已知材料缺口矛盾")


# ---------------------------------------------------------------------------
# 修复 4：冲突待决测量不能换编号进入已确认数值
# ---------------------------------------------------------------------------


def test_conflict_alternative_cannot_be_saved_as_confirmed():
    rows = payloads("gap-conflict")
    paper = find(rows, "gap-conflict-paper")
    for contribution in paper["data"]["object_contributions"]:
        alternative = copy.deepcopy(
            contribution["quantity_conflicts"][0]["alternatives"][0]
        )
        alternative["value_id"] = "duplicate-with-new-id"
        contribution["quantities"][0]["values"].append(alternative)
    with pytest.raises(ValidationError, match="不能同时作为"):
        parse_record(paper)


def test_conflict_evidence_order_cannot_bypass_exclusivity():
    rows = payloads("gap-conflict")
    paper = find(rows, "gap-conflict-paper")
    for contribution in paper["data"]["object_contributions"]:
        value = copy.deepcopy(contribution["quantity_conflicts"][0]["alternatives"][0])
        assert len(value["direct_evidence"]) > 1
        value["value_id"] = "same-measurement-reordered-evidence"
        value["direct_evidence"].reverse()  # 仅证据排列顺序不同
        contribution["quantities"][0]["values"].append(value)
    with pytest.raises(ValidationError, match="不能同时作为"):
        parse_record(paper)


def test_conversion_conflict_evidence_order_cannot_bypass_exclusivity():
    source = load_fixture("gap-conflict")
    source = next(r for r in source if r.RECORD_NAME == "source_package")
    submission = gap_conflict_submission()
    obj = submission["objects"][0]
    value = copy.deepcopy(obj["quantity_conflicts"][0]["alternatives"][0])
    value["evidence"][0]["parts"].reverse()  # 仅填写顺序不同
    obj["quantities"][0]["values"].append(value)
    with pytest.raises(ValueError, match="不能同时作为"):
        convert_quantity_submission(
            make_context(source), QuantitySubmission.model_validate(submission)
        )


def test_independent_equal_measurement_not_confused_with_conflict():
    from records_helpers import (
        RecordRef,
        conflict_pair,
        contribution,
        coverage_rows,
    )

    conflict = conflict_pair(RecordRef(record_id="gap-conflict-question", revision=1))
    confirmed = conflict.alternatives[0].model_dump(mode="json")
    confirmed.update(
        {
            "value_id": "independent",
            "condition": "Independent reanalysis; heliocentric frame.",
            "source": "prior_work",
        }
    )
    # 数值与冲突文本方相同，但条件与来源不同：独立测量，不应被误判为重复
    contribution_with_both = contribution(
        quantities=[
            {
                "quantity": "observed_phase_space.radial_velocity",
                "values": [confirmed],
            }
        ],
        conflicts=[conflict],
        coverage=coverage_rows(),
    )
    assert contribution_with_both.quantities[0].values[0].value == "520"


# ---------------------------------------------------------------------------
# 修复 5：问题指纹按实际内容重算
# ---------------------------------------------------------------------------


def test_stale_decision_after_question_content_change():
    rows = payloads("gap-conflict")
    for question in (row for row in rows if row["schema"]["name"] == "question"):
        # 内容改变但保留旧指纹与旧决定
        question["data"]["category"] = "identity"
        question["data"]["evidence_refs"] = [
            {"record_id": "gap-conflict-ev-identity", "revision": 1}
        ]
        question["data"]["prompt"] = "Do these labels identify one object?"
    check_rejected(rows, "指纹与实际固定内容不一致", "旧决定不能套用新问题")


# ---------------------------------------------------------------------------
# 修复 6：裁决补丁的目标、条目与字段类型
# ---------------------------------------------------------------------------


def test_patch_target_outside_question_scope_rejected():
    rows = payloads("gap-conflict")
    decision = find(rows, "gap-conflict-decision")
    decision["data"]["outcome"] = "resolved"
    decision["data"]["result_patch"] = {
        "target": {
            "record_id": "gap-conflict-roster",
            "revision": 1,
            "item_id": "made-up-entry",
        },
        "changes": [{"field": "invented_field", "value": {"arbitrary": "data"}}],
    }
    check_rejected(rows, "不在问题", "目标范围")


def test_patch_undeclared_item_and_field_rejected():
    rows = payloads("gap-conflict")
    decision = find(rows, "gap-conflict-decision")
    decision["data"]["outcome"] = "resolved"
    decision["data"]["result_patch"] = {
        "target": {
            "record_id": "gap-conflict-paper",  # 在问题目标范围内
            "revision": 1,
            "item_id": "made-up-entry",
        },
        "changes": [{"field": "invented_field", "value": {"arbitrary": "data"}}],
    }
    check_rejected(rows, "不在", "条目")


def test_patch_field_value_type_checked():
    rows = payloads("gap-conflict")
    decision = find(rows, "gap-conflict-decision")
    decision["data"]["outcome"] = "resolved"
    decision["data"]["result_patch"] = {
        "target": {
            "record_id": "gap-conflict-paper",
            "revision": 1,
            "item_id": "contribution-A",
        },
        "changes": [
            # 条目级字段，但值既不符合模型也违反非空约束
            {"field": "contribution_summary", "value": {"arbitrary": "data"}},
            {
                "field": "contribution_summary",
                "value": {"text": "", "evidence_refs": []},
            },
        ],
    }
    check_rejected(rows, "补丁修改后的条目不满足字段或跨字段约束")


def test_patch_record_level_field_rejected_via_item_target():
    rows = payloads("gap-conflict")
    decision = find(rows, "gap-conflict-decision")
    decision["data"]["outcome"] = "resolved"
    decision["data"]["result_patch"] = {
        "target": {
            "record_id": "gap-conflict-paper",
            "revision": 1,
            "item_id": "contribution-A",
        },
        # 记录级字段：清空整篇贡献列表 / 空 paper_key 都不能借条目目标通过
        "changes": [
            {"field": "object_contributions", "value": []},
            {"field": "paper_key", "value": ""},
        ],
    }
    check_rejected(rows, "不能修改记录级字段")


def test_patch_valid_contribution_field_accepted():
    rows = payloads("gap-conflict")
    paper = find(rows, "gap-conflict-paper")
    valid_summary = copy.deepcopy(
        paper["data"]["object_contributions"][0]["contribution_summary"]
    )
    decision = find(rows, "gap-conflict-decision")
    decision["data"]["outcome"] = "resolved"
    decision["data"]["result_patch"] = {
        "target": {
            "record_id": "gap-conflict-paper",
            "revision": 1,
            "item_id": "contribution-A",
        },
        "changes": [{"field": "contribution_summary", "value": valid_summary}],
    }
    check(rows)  # 合法条目字段与取值：整集校验通过


# ---------------------------------------------------------------------------
# 修复 7：发布清单哈希与批准绑定实际内容
# ---------------------------------------------------------------------------


def test_release_approval_binds_actual_manifest_content():
    rows = payloads()
    release = find(rows, "numeric-forms-release")
    parsed = parse_record(release)
    digest = compute_release_manifest_sha256(parsed.data)
    release["data"]["manifest_sha256"] = digest
    release["data"]["approval"] = {
        "approver": "FICTIONAL-EXPERT",
        "approved_at": "2026-09-21T00:00:00Z",
        "manifest_sha256": digest,
        "target": release["data"]["channels"][0]["target"],
    }
    check(rows)  # 真实哈希 + 真实批准：通过
    release["data"]["content_refs"] = []
    with pytest.raises(ValidationError, match="清单哈希与实际待发布内容不一致"):
        parse_record(release)
    # 只重算清单哈希、保留旧批准：批准不再绑定当前内容，必须拒绝
    refresh_release_manifest(release)
    with pytest.raises(ValidationError, match="批准必须绑定当前清单哈希"):
        parse_record(release)


def test_release_manifest_hash_recomputed_from_content():
    rows = payloads()
    release = find(rows, "numeric-forms-release")
    release["data"]["content_refs"] = []
    with pytest.raises(ValidationError, match="清单哈希与实际待发布内容不一致"):
        parse_record(release)


def test_release_approval_target_must_be_declared_channel():
    rows = payloads()
    release = find(rows, "numeric-forms-release")
    parsed = parse_record(release)
    digest = compute_release_manifest_sha256(parsed.data)
    release["data"]["approval"] = {
        "approver": "FICTIONAL-EXPERT",
        "approved_at": "2026-09-21T00:00:00Z",
        "manifest_sha256": digest,
        "target": "undeclared-channel",
    }
    with pytest.raises(ValidationError, match="批准目标必须是已声明的发布渠道之一"):
        parse_record(release)


# ---------------------------------------------------------------------------
# 修复 8：未支持的材料定位不能视为已验证
# ---------------------------------------------------------------------------


def test_unsupported_locator_rejected_as_unverified():
    rows = payloads()
    evidence = find(rows, "numeric-forms-ev-parallax")
    evidence["data"]["locator"]["selector"] = {
        "kind": "unsupported-selector",
        "value": "/parallax",
    }
    check_rejected(rows, "未支持的材料定位选择器")


# ---------------------------------------------------------------------------
# 修复 9：同一物理量的冲突问题与额外问题合并保留
# ---------------------------------------------------------------------------


def test_inline_question_does_not_overwrite_conflict_coverage_link():
    source = load_fixture("gap-conflict")
    source = next(r for r in source if r.RECORD_NAME == "source_package")
    submission = gap_conflict_submission()
    submission["questions"] = [
        {
            "object_id": "contribution-A",
            "quantity": "observed_phase_space.radial_velocity",
            "category": "interpretation",
            "prompt": "Is the velocity frame explicit?",
            "evidence": [],
            "gap_ids": [],
        }
    ]
    result = convert_quantity_submission(
        make_context(source), QuantitySubmission.model_validate(submission)
    )
    obj = result.objects[0]
    conflict_ref = obj.quantity_conflicts[0].issue_ref
    coverage = next(
        row
        for row in obj.quantity_coverage
        if row.quantity == "observed_phase_space.radial_velocity"
    )
    inline_ref = result.questions[-1].ref()
    assert conflict_ref in coverage.issue_refs
    assert inline_ref in coverage.issue_refs
    assert len(coverage.issue_refs) == 2


def test_deduplicated_conflict_and_inline_question_reference():
    """同一条引用不应因来源不同而重复出现。"""
    source = load_fixture("gap-conflict")
    source = next(r for r in source if r.RECORD_NAME == "source_package")
    submission = gap_conflict_submission()
    submission["questions"] = [
        {
            "object_id": "contribution-A",
            "quantity": "observed_phase_space.parallax",
            "category": "interpretation",
            "prompt": "Is the parallax frame explicit?",
            "evidence": [copy.deepcopy(EV_520)],
            "gap_ids": [],
        }
    ]
    result = convert_quantity_submission(
        make_context(source), QuantitySubmission.model_validate(submission)
    )
    coverage = next(
        row
        for row in result.objects[0].quantity_coverage
        if row.quantity == "observed_phase_space.parallax"
    )
    assert len(coverage.issue_refs) == 1


# ---------------------------------------------------------------------------
# 第四轮验收：组合补丁、误差分量内部证据顺序、补丁引用核对
# ---------------------------------------------------------------------------


def _rebind_decision_to_roster_entry(rows: list[dict]) -> dict:
    """把夹具问题与决定改绑到名单条目 roster-A，返回补丁目标。"""
    from hvs_catalog.records.questions import compute_question_fingerprint

    roster = find(rows, "gap-conflict-roster")
    entry = roster["data"]["entries"][0]
    target = {
        "record_id": roster["record_id"],
        "revision": roster["revision"],
        "item_id": entry["item_id"],
    }
    question = find(rows, "gap-conflict-question")  # 第一处即 r1
    question["data"]["category"] = "inclusion"
    question["data"]["target_refs"] = [target]
    question["data"]["prompt"] = "Should this fictional entry be excluded?"
    parsed = parse_record(question).data
    fingerprint = compute_question_fingerprint(
        category=parsed.category,
        target_refs=parsed.target_refs,
        evidence_refs=parsed.evidence_refs,
        source_refs=parsed.source_refs,
        rules_ref=parsed.rules_ref,
    )
    question["data"]["fingerprint"] = fingerprint
    decision = find(rows, "gap-conflict-decision")
    decision["data"]["question_fingerprint"] = fingerprint
    decision["data"]["outcome"] = "resolved"
    return target


@pytest.mark.parametrize("reverse_changes", [False, True])
def test_joint_roster_patch_accepted_in_both_orders(reverse_changes):
    """收录改排除并清空贡献编号：合法组合修改不因中间状态被拒。"""
    rows = payloads("gap-conflict")
    target = _rebind_decision_to_roster_entry(rows)
    check(rows)  # 改绑后、加补丁前集合本身合法
    changes = [
        {"field": "disposition", "value": "excluded"},
        {"field": "contribution_id", "value": None},
    ]
    if reverse_changes:
        changes.reverse()
    find(rows, "gap-conflict-decision")["data"]["result_patch"] = {
        "target": target,
        "changes": changes,
    }
    check(rows)  # 最终条目 excluded 且无贡献编号：通过


def test_joint_patch_with_invalid_final_state_rejected():
    rows = payloads("gap-conflict")
    target = _rebind_decision_to_roster_entry(rows)
    find(rows, "gap-conflict-decision")["data"]["result_patch"] = {
        "target": target,
        # 排除但保留贡献编号：任一顺序都无法得到合法终态
        "changes": [{"field": "disposition", "value": "excluded"}],
    }
    check_rejected(rows, "不满足字段或跨字段约束")


def _component_bearing_alternative(value: dict) -> dict:
    """给冲突方添加合法的具名误差分量（两条分量证据来自同一原文）。"""
    evidence = next(
        part for part in value["direct_evidence"] if part["part"] == "error"
    )
    lower = copy.deepcopy(evidence)
    lower["part"] = "lower_error"
    upper = copy.deepcopy(evidence)
    upper["part"] = "upper_error"
    value["error_components"] = [
        {
            "name": "synthetic-component",
            "error": None,
            "lower_error": "10",
            "upper_error": "10",
            "direct_evidence": [lower, upper],
        }
    ]
    return value


def test_nested_component_evidence_order_saved_entry():
    rows = payloads("gap-conflict")
    for paper in (row for row in rows if row["schema"]["name"] == "paper"):
        alternative = paper["data"]["object_contributions"][0]["quantity_conflicts"][0][
            "alternatives"
        ][0]
        _component_bearing_alternative(alternative)
    check(rows)  # 带分量的冲突本身合法
    paper = find(rows, "gap-conflict-paper")
    for contribution in paper["data"]["object_contributions"]:
        duplicate = copy.deepcopy(
            contribution["quantity_conflicts"][0]["alternatives"][0]
        )
        duplicate["value_id"] = "duplicate-nested-order"
        duplicate["error_components"][0]["direct_evidence"].reverse()  # 仅分量内顺序
        contribution["quantities"][0]["values"].append(duplicate)
    with pytest.raises(ValidationError, match="不能同时作为"):
        parse_record(paper)


def test_nested_component_evidence_order_conversion():
    source = load_fixture("gap-conflict")
    source = next(r for r in source if r.RECORD_NAME == "source_package")
    submission = gap_conflict_submission()
    obj = submission["objects"][0]
    value = obj["quantity_conflicts"][0]["alternatives"][0]
    evidence = copy.deepcopy(value["evidence"][0])
    evidence["parts"] = [
        {"part": "lower_error", "raw_fragment": "10"},
        {"part": "upper_error", "raw_fragment": "10"},
    ]
    value["error_components"] = [
        {
            "name": "synthetic-component",
            "lower_error": "10",
            "upper_error": "10",
            "evidence": [evidence],
        }
    ]
    convert_quantity_submission(
        make_context(source), QuantitySubmission.model_validate(submission)
    )  # 带分量的冲突填写本身合法
    duplicate = copy.deepcopy(value)
    duplicate["error_components"][0]["evidence"][0]["parts"].reverse()  # 仅分量内顺序
    obj["quantities"][0]["values"].append(duplicate)
    with pytest.raises(ValueError, match="不能同时作为"):
        convert_quantity_submission(
            make_context(source), QuantitySubmission.model_validate(submission)
        )


def test_patch_missing_evidence_reference_rejected():
    rows = payloads("gap-conflict")
    paper = find(rows, "gap-conflict-paper")
    item = paper["data"]["object_contributions"][0]
    summary = copy.deepcopy(item["contribution_summary"])
    summary["evidence_refs"] = [{"record_id": "evidence-does-not-exist", "revision": 1}]
    decision = find(rows, "gap-conflict-decision")
    decision["data"]["outcome"] = "resolved"
    decision["data"]["result_patch"] = {
        "target": {
            "record_id": paper["record_id"],
            "revision": paper["revision"],
            "item_id": item["item_id"],
        },
        "changes": [{"field": "contribution_summary", "value": summary}],
    }
    check_rejected(rows, "引用的记录不存在")


def test_patch_wrong_type_evidence_reference_rejected():
    rows = payloads("gap-conflict")
    paper = find(rows, "gap-conflict-paper")
    item = paper["data"]["object_contributions"][0]
    summary = copy.deepcopy(item["contribution_summary"])
    summary["evidence_refs"] = [{"record_id": "gap-conflict-source", "revision": 1}]
    decision = find(rows, "gap-conflict-decision")
    decision["data"]["outcome"] = "resolved"
    decision["data"]["result_patch"] = {
        "target": {
            "record_id": paper["record_id"],
            "revision": paper["revision"],
            "item_id": item["item_id"],
        },
        "changes": [{"field": "contribution_summary", "value": summary}],
    }
    check_rejected(rows, "应指向 evidence 记录")


def test_patch_foreign_paper_evidence_rejected():
    rows = payloads("gap-conflict")
    source = copy.deepcopy(find(rows, "gap-conflict-source"))
    source["record_id"] = "foreign-source"
    source["data"]["paper_key"] = "FOREIGN-PAPER"
    rows.append(source)
    evidence = copy.deepcopy(find(rows, "gap-conflict-ev-values"))
    evidence["record_id"] = "foreign-evidence"
    evidence["data"]["paper_key"] = "FOREIGN-PAPER"
    evidence["data"]["source_ref"] = {"record_id": "foreign-source", "revision": 1}
    rows.append(evidence)
    paper = find(rows, "gap-conflict-paper")
    item = paper["data"]["object_contributions"][0]
    summary = copy.deepcopy(item["contribution_summary"])
    summary["evidence_refs"] = [{"record_id": "foreign-evidence", "revision": 1}]
    decision = find(rows, "gap-conflict-decision")
    decision["data"]["outcome"] = "resolved"
    decision["data"]["result_patch"] = {
        "target": {
            "record_id": paper["record_id"],
            "revision": paper["revision"],
            "item_id": item["item_id"],
        },
        "changes": [{"field": "contribution_summary", "value": summary}],
    }
    check_rejected(rows, "另一篇论文的证据")


def test_patch_numeric_value_reuses_fragment_containment_check():
    rows = payloads("gap-conflict")
    paper = find(rows, "gap-conflict-paper")
    item = paper["data"]["object_contributions"][0]
    value = copy.deepcopy(item["quantities"][0]["values"][0])
    value["value_id"] = "patched-value"
    value["value"] = "9999"  # 数值与片段一致，但原文里没有该片段
    for part in value["direct_evidence"]:
        if part["part"] == "value":
            part["raw_fragment"] = "9999"
    decision = find(rows, "gap-conflict-decision")
    decision["data"]["outcome"] = "resolved"
    decision["data"]["result_patch"] = {
        "target": {
            "record_id": paper["record_id"],
            "revision": paper["revision"],
            "item_id": item["item_id"],
        },
        "changes": [
            {
                "field": "quantities",
                "value": [
                    {
                        "quantity": item["quantities"][0]["quantity"],
                        "values": [value],
                    }
                ],
            }
        ],
    }
    check_rejected(rows, "不在证据")
