"""Acceptance counterexample regressions for references, values, gaps, conflicts,
fingerprints, patches, releases, and locators.

Build every case from fictional shared-record fixtures and cover both submission
conversion and direct saved-record loading.
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
# Fix 1: scientific evidence reference types, paper ownership, and exact revisions.
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
    # External evidence has valid source, excerpt, and locator; only paper ownership is wrong.
    check_rejected(rows, "另一篇论文的证据")


def test_fragment_checked_against_exact_evidence_revision():
    rows = payloads()
    evidence = copy.deepcopy(find(rows, "numeric-forms-ev-values"))
    evidence["revision"] = 2
    rows.append(evidence)
    value = find(rows, "numeric-forms-paper")["data"]["object_contributions"][0][
        "quantities"
    ][0]["values"][0]
    # Value and fragment match, but the fragment is absent from v1; new v2 cannot hide the v1 defect.
    value["value"] = "9999"
    value["direct_evidence"][0]["raw_fragment"] = "9999"
    validate_collection([parse_record(row) for row in rows])


# ---------------------------------------------------------------------------
# Fix 2: match values to printed fragments.
# ---------------------------------------------------------------------------


def test_saved_number_must_equal_printed_fragment():
    rows = payloads()
    value = find(rows, "numeric-forms-paper")["data"]["object_contributions"][0][
        "quantities"
    ][0]["values"][0]
    value["value"] = "999999"  # Original text and numeric evidence still say 520.
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
# Fix 3: gap scope and both entry points.
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
                "gap_ids": [
                    "gap-velocity"
                ],  # The known-scope gap affects value extraction only.
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
    # Inclusion gaps do not affect quantity completion: all 18 rows are complete without judgment gaps.
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
# Fix 4: renaming a pending conflicting measurement cannot turn it into a confirmed value.
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
        value["direct_evidence"].reverse()  # Only evidence order differs.
        contribution["quantities"][0]["values"].append(value)
    with pytest.raises(ValidationError, match="不能同时作为"):
        parse_record(paper)


def test_conversion_conflict_evidence_order_cannot_bypass_exclusivity():
    source = load_fixture("gap-conflict")
    source = next(r for r in source if r.RECORD_NAME == "source_package")
    submission = gap_conflict_submission()
    obj = submission["objects"][0]
    value = copy.deepcopy(obj["quantity_conflicts"][0]["alternatives"][0])
    value["evidence"][0]["parts"].reverse()  # Only submission order differs.
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
    # Same value as a conflict alternative but different conditions/source: an independent measurement, not a duplicate.
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
# Fix 5: recompute question fingerprints from actual contents.
# ---------------------------------------------------------------------------


def test_stale_decision_after_question_content_change():
    rows = payloads("gap-conflict")
    for question in (row for row in rows if row["schema"]["name"] == "question"):
        # Change contents while retaining the old fingerprint and decision.
        question["data"]["category"] = "identity"
        question["data"]["evidence_refs"] = [
            {"record_id": "gap-conflict-ev-identity", "revision": 1}
        ]
        question["data"]["prompt"] = "Do these labels identify one object?"
    check_rejected(rows, "指纹与实际固定内容不一致", "旧决定不能套用新问题")


# ---------------------------------------------------------------------------
# Fix 6: decision patch targets, items, and field types.
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
            "record_id": "gap-conflict-paper",  # Within the question's target scope.
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
            # An item field whose value violates both the model and nonempty constraint.
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
        # Record-level fields cannot clear contributions or paper_key through an item target.
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
    check(rows)  # Valid item fields and values pass collection validation.


# ---------------------------------------------------------------------------
# Fix 7: release manifest hashes and approvals bind actual contents.
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
    check(rows)  # Actual hash and approval pass.
    release["data"]["content_refs"] = []
    with pytest.raises(ValidationError, match="清单哈希与实际待发布内容不一致"):
        parse_record(release)
    # Recomputed manifest with old approval no longer binds current contents and must reject.
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
# Fix 8: unsupported material locators cannot count as verified.
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
# Fix 9: retain both conflict questions and additional quantity questions.
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
    """Do not duplicate the same reference merely because its origins differ."""
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
# Combined patches, component-evidence order, and patch reference checks.
# ---------------------------------------------------------------------------


def _rebind_decision_to_roster_entry(rows: list[dict]) -> dict:
    """Rebind fixture questions/decisions to roster-A and return the patch target."""
    from hvs_catalog.records.questions import compute_question_fingerprint

    roster = find(rows, "gap-conflict-roster")
    entry = roster["data"]["entries"][0]
    target = {
        "record_id": roster["record_id"],
        "revision": roster["revision"],
        "item_id": entry["item_id"],
    }
    question = find(rows, "gap-conflict-question")  # The first location is r1.
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
    """Changing included to excluded and clearing contribution IDs is a valid combined
    update despite invalid intermediate states.
    """
    rows = payloads("gap-conflict")
    target = _rebind_decision_to_roster_entry(rows)
    check(rows)  # The rebound collection is valid before adding patches.
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
    check(rows)  # The final excluded entry without contribution IDs is valid.


def test_joint_patch_with_invalid_final_state_rejected():
    rows = payloads("gap-conflict")
    target = _rebind_decision_to_roster_entry(rows)
    find(rows, "gap-conflict-decision")["data"]["result_patch"] = {
        "target": target,
        # Exclusion retaining contribution IDs cannot produce a valid final state in either order.
        "changes": [{"field": "disposition", "value": "excluded"}],
    }
    check_rejected(rows, "不满足字段或跨字段约束")


def _component_bearing_alternative(value: dict) -> dict:
    """Add valid named error components to conflict alternatives, with both evidence
    fragments from the same source text.
    """
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
    check(rows)  # The conflict with components is valid.
    paper = find(rows, "gap-conflict-paper")
    for contribution in paper["data"]["object_contributions"]:
        duplicate = copy.deepcopy(
            contribution["quantity_conflicts"][0]["alternatives"][0]
        )
        duplicate["value_id"] = "duplicate-nested-order"
        duplicate["error_components"][0][
            "direct_evidence"
        ].reverse()  # Only order inside components differs.
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
    )  # Conflict submission with components is valid.
    duplicate = copy.deepcopy(value)
    duplicate["error_components"][0]["evidence"][0][
        "parts"
    ].reverse()  # Only order inside components differs.
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


def test_patch_keeps_evidence_without_a_numeric_containment_gate():
    rows = payloads("gap-conflict")
    paper = find(rows, "gap-conflict-paper")
    item = paper["data"]["object_contributions"][0]
    value = copy.deepcopy(item["quantities"][0]["values"][0])
    value["value_id"] = "patched-value"
    value["value"] = (
        "9999"  # Value and fragment match, but the fragment is absent from source text.
    )
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
    validate_collection([parse_record(row) for row in rows])
