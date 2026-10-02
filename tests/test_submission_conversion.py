"""Convert simplified submissions to saved records: expansion, identifiers, evidence
generation, and rejection cases.
"""

from __future__ import annotations

import copy

import pytest
from records_helpers import STAMP, fixture_by_name, load_fixture

from hvs_catalog.records import (
    StepContext,
    convert_candidate_submission,
    convert_quantity_submission,
    convert_roster_submission,
    quantity_paths,
)
from hvs_catalog.records.submission import (
    CandidateSubmission,
    QuantitySubmission,
    RosterSubmission,
)

LINE2 = (
    "Our new spectrum gives a heliocentric radial velocity of 520 ± 10 km/s for FIC-A. "
    "Previous Study reported 550 ± 20 km/s in the same frame."
)


def make_context(source, object_ids=("contribution-A",)):
    paper = fixture_by_name(load_fixture("gap-conflict"), "paper")
    from hvs_catalog.records import RecordRef

    return StepContext(
        source=source,
        paper_ref=RecordRef(record_id=paper.record_id, revision=1),
        created_at=STAMP,
        requested_quantities=quantity_paths(),
        object_ids=object_ids,
        id_prefix="t",
    )


def inline_text(line, excerpt, parts, context=True):
    return {
        "source": {
            "file_id": "main",
            "locator": {"kind": "text", "start_line": line, "end_line": line},
            "excerpt": excerpt,
        },
        "parts": parts,
        "context": context,
    }


def inline_table(column, excerpt, parts):
    return {
        "source": {
            "file_id": "table",
            "locator": {
                "kind": "table_cell",
                "row_key": {"object": "FIC-A"},
                "column": column,
                "row_number": 1,
            },
            "excerpt": excerpt,
        },
        "parts": parts,
        "context": False,
    }


def compact_value(value, error, condition, source_kind, evidence):
    evidence = copy.deepcopy(
        evidence
    )  # Each case modifies its own independent evidence.
    payload = {
        "value": value,
        "error": error,
        "unit": "km/s",
        "limit_kind": "none",
        "condition": condition,
        "frame": "heliocentric",
        "purpose": "measurement",
        "source": source_kind,
        "evidence": evidence,
    }
    return payload


EV_520 = inline_text(
    2,
    LINE2,
    [{"part": "value", "raw_fragment": "520"}, {"part": "error", "raw_fragment": "10"}],
)
EV_550 = inline_text(
    2,
    LINE2,
    [{"part": "value", "raw_fragment": "550"}, {"part": "error", "raw_fragment": "20"}],
)
EV_525 = inline_table(
    "heliocentric_rv", "525", [{"part": "value", "raw_fragment": "525"}]
)
EV_525E = inline_table("error", "10", [{"part": "error", "raw_fragment": "10"}])


def gap_conflict_submission():
    return {
        "objects": [
            {
                "object_id": "contribution-A",
                "quantities": [
                    {
                        "quantity": "observed_phase_space.radial_velocity",
                        "values": [
                            compact_value(
                                "550",
                                "20",
                                "Previous Study quoted for comparison; heliocentric frame.",
                                "prior_work",
                                [EV_550],
                            )
                        ],
                    }
                ],
                "quantity_conflicts": [
                    {
                        "quantity": "observed_phase_space.radial_velocity",
                        "reason": "The text and table report different values for the same new observation.",
                        "alternatives": [
                            compact_value(
                                "520",
                                "10",
                                "New spectrum; heliocentric frame.",
                                "this_paper",
                                [EV_520],
                            ),
                            compact_value(
                                "525",
                                "10",
                                "New spectrum; heliocentric frame.",
                                "this_paper",
                                [EV_525, EV_525E],
                            ),
                        ],
                    }
                ],
                "coverage": {
                    "scope": "all_requested",
                    "common": {
                        "state": "complete",
                        "checked_file_ids": ["main", "table"],
                        "gap_ids": [],
                        "issue_ids": [],
                    },
                    "exceptions": [
                        {
                            "quantity": "observed_phase_space.radial_velocity",
                            "state": "partial",
                            "checked_file_ids": ["main", "table"],
                            "gap_ids": ["gap-velocity"],
                            "issue_ids": [],
                        }
                    ],
                },
            }
        ],
        "questions": [],
    }


@pytest.fixture()
def gap_source():
    return fixture_by_name(load_fixture("gap-conflict"), "source_package")


def test_full_expansion_and_references(gap_source):
    result = convert_quantity_submission(
        make_context(gap_source),
        QuantitySubmission.model_validate(gap_conflict_submission()),
    )
    obj = result.objects[0]
    assert len(obj.quantity_coverage) == 18

    rv = next(
        row
        for row in obj.quantity_coverage
        if row.quantity == "observed_phase_space.radial_velocity"
    )
    others = [
        row
        for row in obj.quantity_coverage
        if row.quantity != "observed_phase_space.radial_velocity"
    ]
    assert rv.state == "partial"
    assert [gap.gap_id for gap in rv.gap_refs] == ["gap-velocity"]
    assert len(rv.issue_refs) == 1  # Questions generated from conflicts.
    assert all(row.state == "complete" and not row.gap_refs for row in others)

    conflict = obj.quantity_conflicts[0]
    assert len(conflict.alternatives) == 2
    assert conflict.issue_ref == rv.issue_refs[0]
    question = result.questions[0]
    assert question.data.category == "numeric_conflict"
    assert question.data.status == "open"

    # Conflict alternatives stay outside ordinary value groups; both source value groups coexist.
    confirmed = obj.quantities[0].values[0]
    assert confirmed.source == "prior_work"
    assert {value.value for value in conflict.alternatives} == {"520", "525"}

    # Merge evidence: two value groups at the same body location share one evidence record.
    text_evidence = [r for r in result.evidence if r.data.file_id == "main"]
    assert len(text_evidence) == 1
    assert len(result.evidence) == 3

    # Saved representations include every key.
    saved = conflict.alternatives[0]
    assert (
        saved.coverage is None and saved.epoch is None and saved.error_components == []
    )


def test_missing_coverage_declaration_rejected(gap_source):
    payload = gap_conflict_submission()
    del payload["objects"][0]["coverage"]
    with pytest.raises(ValueError):
        convert_quantity_submission(
            make_context(gap_source), QuantitySubmission.model_validate(payload)
        )


def test_common_state_omitted_rejected(gap_source):
    payload = gap_conflict_submission()
    del payload["objects"][0]["coverage"]["common"]["state"]
    with pytest.raises(ValueError):
        convert_quantity_submission(
            make_context(gap_source), QuantitySubmission.model_validate(payload)
        )


def test_unknown_exception_quantity_rejected(gap_source):
    payload = gap_conflict_submission()
    payload["objects"][0]["coverage"]["exceptions"][0]["quantity"] = (
        "fictional.quantity"
    )
    with pytest.raises(ValueError, match="未知的物理量编码|未请求的物理量"):
        convert_quantity_submission(
            make_context(gap_source), QuantitySubmission.model_validate(payload)
        )


def test_duplicate_exception_rejected(gap_source):
    payload = gap_conflict_submission()
    payload["objects"][0]["coverage"]["exceptions"] *= 2
    with pytest.raises(ValueError, match="例外声明重复"):
        convert_quantity_submission(
            make_context(gap_source), QuantitySubmission.model_validate(payload)
        )


def test_unknown_checked_file_rejected(gap_source):
    payload = gap_conflict_submission()
    payload["objects"][0]["coverage"]["common"]["checked_file_ids"] = ["invented-file"]
    with pytest.raises(ValueError, match="不存在的文件"):
        convert_quantity_submission(
            make_context(gap_source), QuantitySubmission.model_validate(payload)
        )


def test_complete_contradicting_known_gap_rejected(gap_source):
    payload = gap_conflict_submission()
    payload["objects"][0]["coverage"]["exceptions"] = []
    with pytest.raises(ValueError, match="完成声明与已知材料缺口矛盾"):
        convert_quantity_submission(
            make_context(gap_source), QuantitySubmission.model_validate(payload)
        )


def test_unknown_gap_scope_blocks_all_complete(gap_source):
    payload = gap_conflict_submission()
    payload["objects"][0]["coverage"]["exceptions"] = [
        {
            "quantity": "observed_phase_space.radial_velocity",
            "state": "partial",
            "checked_file_ids": ["main", "table"],
            "gap_ids": [],
            "issue_ids": [],
        }
    ]
    source = gap_source.model_copy(deep=True)
    gap = source.data.gaps[0].model_copy(deep=True)
    gap.affected_targets = {"scope": "unknown", "items": []}
    source.data.gaps[0] = gap
    with pytest.raises(ValueError, match="影响范围未知|完成声明与已知材料缺口矛盾"):
        convert_quantity_submission(
            make_context(source), QuantitySubmission.model_validate(payload)
        )


def test_gap_listed_where_scope_says_unaffected_rejected(gap_source):
    payload = gap_conflict_submission()
    payload["objects"][0]["coverage"]["common"]["gap_ids"] = ["gap-velocity"]
    with pytest.raises(ValueError, match="不影响该物理量|完成声明与已知材料缺口矛盾"):
        convert_quantity_submission(
            make_context(gap_source), QuantitySubmission.model_validate(payload)
        )


def test_unknown_gap_or_issue_reference_rejected(gap_source):
    payload = gap_conflict_submission()
    payload["objects"][0]["coverage"]["exceptions"][0]["gap_ids"] = ["gap-fictional"]
    with pytest.raises(ValueError, match="不存在的材料缺口"):
        convert_quantity_submission(
            make_context(gap_source), QuantitySubmission.model_validate(payload)
        )
    payload = gap_conflict_submission()
    payload["objects"][0]["coverage"]["common"]["issue_ids"] = ["issue-fictional"]
    with pytest.raises(ValueError, match="不存在的问题"):
        convert_quantity_submission(
            make_context(gap_source), QuantitySubmission.model_validate(payload)
        )


def test_missing_object_response_rejected(gap_source):
    payload = {"objects": [], "questions": []}
    with pytest.raises(ValueError, match="缺少对象的填写结果"):
        convert_quantity_submission(
            make_context(gap_source), QuantitySubmission.model_validate(payload)
        )


def test_unknown_object_identifier_rejected(gap_source):
    payload = gap_conflict_submission()
    extra = {
        "object_id": "contribution-fictional",
        "quantities": [],
        "quantity_conflicts": [],
        "coverage": {
            "scope": "all_requested",
            "common": {
                "state": "not_checked",
                "checked_file_ids": [],
                "gap_ids": [],
                "issue_ids": [],
            },
            "exceptions": [],
        },
    }
    payload["objects"].append(extra)
    with pytest.raises(ValueError, match="未提供的对象编号"):
        convert_quantity_submission(
            make_context(gap_source), QuantitySubmission.model_validate(payload)
        )


def test_duplicated_object_response_rejected(gap_source):
    payload = gap_conflict_submission()
    payload["objects"] *= 2
    with pytest.raises(ValueError, match="填写结果重复"):
        convert_quantity_submission(
            make_context(gap_source), QuantitySubmission.model_validate(payload)
        )


def test_evidence_fragment_must_occur_in_excerpt(gap_source):
    payload = gap_conflict_submission()
    payload["objects"][0]["quantities"][0]["values"][0]["evidence"][0]["parts"] = [
        {"part": "value", "raw_fragment": "9999"}
    ]
    with pytest.raises(ValueError, match="不在所给原文片段"):
        convert_quantity_submission(
            make_context(gap_source), QuantitySubmission.model_validate(payload)
        )


def test_numeric_form_violations_rejected(gap_source):
    payload = gap_conflict_submission()
    payload["objects"][0]["quantities"][0]["values"][0].update(
        {"error": "20", "lower_error": "1", "upper_error": "2"}
    )
    with pytest.raises(ValueError, match="对称误差和非对称误差"):
        convert_quantity_submission(
            make_context(gap_source), QuantitySubmission.model_validate(payload)
        )

    payload = gap_conflict_submission()
    payload["objects"][0]["quantities"][0]["values"][0].update(
        {
            "value": None,
            "limit_kind": "range",
            "range_lower": "7",
            "range_upper": None,
            "interval_kind": "credible",
        }
    )
    with pytest.raises(ValueError, match="两端端点"):
        convert_quantity_submission(
            make_context(gap_source), QuantitySubmission.model_validate(payload)
        )


def test_interval_forms_normalize_with_nulls(gap_source):
    payload = gap_conflict_submission()
    interval = {
        "value": None,
        "limit_kind": "range",
        "range_lower": "7",
        "range_upper": "10",
        "interval_kind": "credible",
        "unit": "kpc",
        "condition": "Model A.",
        "source": "this_paper",
        "coverage": {
            "value": "95",
            "unit": "%",
            "evidence": [
                inline_text(
                    4,
                    "For FIC-A under Model A, the heliocentric distance has a 95% credible interval from 7 to 10 kpc.",
                    [{"part": "value", "raw_fragment": "95"}],
                )
            ],
        },
        "evidence": [
            inline_text(
                4,
                "For FIC-A under Model A, the heliocentric distance has a 95% credible interval from 7 to 10 kpc.",
                [
                    {"part": "range_lower", "raw_fragment": "7"},
                    {"part": "range_upper", "raw_fragment": "10"},
                ],
            )
        ],
    }
    payload["objects"][0]["quantities"].append(
        {"quantity": "observed_phase_space.distance", "values": [interval]}
    )
    result = convert_quantity_submission(
        make_context(gap_source), QuantitySubmission.model_validate(payload)
    )
    distance = next(
        value
        for group in result.objects[0].quantities
        if group.quantity == "observed_phase_space.distance"
        for value in group.values
    )
    assert distance.value is None
    assert distance.range_lower == "7" and distance.range_upper == "10"
    assert distance.interval_kind == "credible"
    assert distance.coverage.value == "95"
    assert distance.error is None and distance.error_components == []


def test_error_components_normalize(gap_source):
    payload = gap_conflict_submission()
    line7 = (
        "For FIC-A under Model D, the heliocentric distance is 9.0 kpc. The authors "
        "report a total uncertainty of 0.5 kpc and named components of 0.3 kpc "
        "(statistical) and 0.4 kpc (systematic)."
    )
    payload["objects"][0]["quantities"].append(
        {
            "quantity": "observed_phase_space.distance",
            "values": [
                {
                    "value": "9.0",
                    "error": "0.5",
                    "unit": "kpc",
                    "limit_kind": "none",
                    "condition": "Model D.",
                    "source": "this_paper",
                    "error_components": [
                        {
                            "name": "statistical",
                            "error": "0.3",
                            "evidence": [
                                inline_text(
                                    7,
                                    line7,
                                    [{"part": "error", "raw_fragment": "0.3"}],
                                    context=False,
                                )
                            ],
                        }
                    ],
                    "evidence": [
                        inline_text(
                            7,
                            line7,
                            [
                                {"part": "value", "raw_fragment": "9.0"},
                                {"part": "error", "raw_fragment": "0.5"},
                            ],
                        )
                    ],
                }
            ],
        }
    )
    result = convert_quantity_submission(
        make_context(gap_source), QuantitySubmission.model_validate(payload)
    )
    distance = next(
        value
        for group in result.objects[0].quantities
        if group.quantity == "observed_phase_space.distance"
        for value in group.values
    )
    assert distance.error == "0.5"
    assert distance.error_components[0].error == "0.3"


def test_inline_question_created_open(gap_source):
    payload = gap_conflict_submission()
    payload["questions"] = [
        {
            "object_id": "contribution-A",
            "quantity": "observed_phase_space.distance",
            "category": "interpretation",
            "prompt": "Which distance definition does the paper adopt?",
            "evidence": [],
        }
    ]
    result = convert_quantity_submission(
        make_context(gap_source), QuantitySubmission.model_validate(payload)
    )
    assert len(result.questions) == 2  # Conflict question plus a new question.
    inline = next(q for q in result.questions if q.data.category == "interpretation")
    assert inline.data.status == "open"
    distance_row = next(
        row
        for row in result.objects[0].quantity_coverage
        if row.quantity == "observed_phase_space.distance"
    )
    assert inline.ref() in distance_row.issue_refs


def test_unknown_quantity_group_rejected(gap_source):
    payload = gap_conflict_submission()
    payload["objects"][0]["quantities"][0]["quantity"] = "fictional.quantity"
    with pytest.raises(ValueError, match="未知的物理量编码"):
        convert_quantity_submission(
            make_context(gap_source), QuantitySubmission.model_validate(payload)
        )


def test_empty_values_group_rejected(gap_source):
    payload = gap_conflict_submission()
    payload["objects"][0]["quantities"][0]["values"] = []
    with pytest.raises(ValueError):
        convert_quantity_submission(
            make_context(gap_source), QuantitySubmission.model_validate(payload)
        )


def test_identical_conflict_alternatives_rejected(gap_source):
    payload = gap_conflict_submission()
    payload["objects"][0]["quantity_conflicts"][0]["alternatives"] = [
        compact_value(
            "520", "10", "New spectrum; heliocentric frame.", "this_paper", [EV_520]
        ),
        compact_value(
            "520", "10", "New spectrum; heliocentric frame.", "this_paper", [EV_520]
        ),
    ]
    with pytest.raises(ValueError, match="两个不同的完整数值表达"):
        convert_quantity_submission(
            make_context(gap_source), QuantitySubmission.model_validate(payload)
        )


# ---------------------------------------------------------------------------
# Roster and candidate steps.
# ---------------------------------------------------------------------------


def roster_submission():
    identity = inline_text(
        1,
        "We selected FIC-A for follow-up spectroscopy because it had previously been "
        "identified as a Galactic unbound candidate.",
        [],
        context=False,
    )
    assessment = inline_text(
        3,
        "Our new observations and analysis lead us to conclude that FIC-A is bound to the Milky Way.",
        [],
        context=False,
    )
    values = inline_text(
        2,
        LINE2,
        [{"part": "value", "raw_fragment": "520"}],
        context=False,
    )
    return {
        "coverage": {"state": "complete", "checked_file_ids": ["main"], "gap_ids": []},
        "entries": [
            {
                "identifiers": [{"value": "FIC-A", "evidence": [identity]}],
                "disposition": "included",
                "evidence": [identity],
                "contribution": {
                    "summary": {
                        "text": "Follow-up spectroscopy of FIC-A.",
                        "evidence": [identity, values],
                    },
                    "contribution_type": {"value": "follow_up", "evidence": [identity]},
                    "boundness": {"status": "bound", "evidence": [assessment]},
                    "conditional_assessments": [],
                },
            }
        ],
        "unresolved_groups": [],
    }


def roster_context(source):
    return StepContext(
        source=source,
        paper_ref=fixture_by_name(load_fixture("gap-conflict"), "paper").ref(),
        created_at=STAMP,
        id_prefix="t",
    )


def test_roster_conversion(gap_source):
    result = convert_roster_submission(
        roster_context(gap_source), RosterSubmission.model_validate(roster_submission())
    )
    entry = result.roster.entries[0]
    contribution = result.contributions[0]
    assert entry.disposition == "included"
    assert entry.contribution_id == contribution.item_id
    assert contribution.roster_item_id == entry.item_id
    assert contribution.paper_boundness.status == "bound"
    assert len(contribution.quantity_coverage) == 18
    assert all(row.state == "not_checked" for row in contribution.quantity_coverage)
    assert contribution.quantities == []  # Roster steps create no empty value groups.
    assert result.roster.method == "direct"


def test_roster_pending_assessment_creates_question(gap_source):
    payload = roster_submission()
    payload["entries"][0]["contribution"]["boundness"] = {
        "status": None,
        "question": {
            "category": "author_assessment",
            "prompt": "Which assessment do the authors finally adopt?",
            "evidence": [],
        },
    }
    result = convert_roster_submission(
        roster_context(gap_source), RosterSubmission.model_validate(payload)
    )
    contribution = result.contributions[0]
    assert contribution.paper_boundness.status is None
    assert len(contribution.paper_boundness.issue_refs) == 1
    assert result.questions[0].data.category == "author_assessment"


def test_roster_excluded_needs_evidence(gap_source):
    payload = roster_submission()
    payload["entries"][0] = {
        "identifiers": payload["entries"][0]["identifiers"],
        "disposition": "excluded",
        "evidence": [],
    }
    with pytest.raises(ValueError, match="排除"):
        convert_roster_submission(
            roster_context(gap_source), RosterSubmission.model_validate(payload)
        )


def test_roster_pending_needs_question(gap_source):
    payload = roster_submission()
    payload["entries"][0] = {
        "identifiers": payload["entries"][0]["identifiers"],
        "disposition": "pending",
        "evidence": [inline_text(1, "Some passage.", [], context=False)],
        "question": None,
    }
    with pytest.raises(ValueError, match="待决"):
        convert_roster_submission(
            roster_context(gap_source), RosterSubmission.model_validate(payload)
        )


def test_roster_unresolved_member_must_be_entry(gap_source):
    payload = roster_submission()
    payload["unresolved_groups"] = [
        {
            "original_range": "stars 1-5",
            "evidence": [
                inline_text(1, "Some passage about stars 1-5.", [], context=False)
            ],
            "member_identifier_values": ["FIC-B"],
            "reason": "The paper does not list every member in the supplied materials.",
        }
    ]
    with pytest.raises(ValueError, match="不在名单条目中"):
        convert_roster_submission(
            roster_context(gap_source), RosterSubmission.model_validate(payload)
        )


def test_candidate_judgments_must_cover_given(gap_source):
    judgment = {
        "item_id": "cand-1",
        "disposition": "excluded",
        "evidence": [
            inline_text(1, "Label only; no qualifying evidence.", [], context=False)
        ],
    }
    submission = CandidateSubmission.model_validate({"judgments": [judgment]})
    result = convert_candidate_submission(
        roster_context(gap_source), ["cand-1"], submission
    )
    assert result.judgments[0].disposition == "excluded"
    assert result.judgments[0].contribution_id is None

    with pytest.raises(ValueError, match="缺|多"):
        convert_candidate_submission(
            roster_context(gap_source), ["cand-1", "cand-2"], submission
        )

    extra = CandidateSubmission.model_validate(
        {
            "judgments": [
                judgment,
                {
                    "item_id": "cand-2",
                    "disposition": "excluded",
                    "evidence": judgment["evidence"],
                },
                {
                    "item_id": "cand-3",
                    "disposition": "excluded",
                    "evidence": judgment["evidence"],
                },
            ]
        }
    )
    with pytest.raises(ValueError, match="缺|多"):
        convert_candidate_submission(
            roster_context(gap_source), ["cand-1", "cand-2"], extra
        )
