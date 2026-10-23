"""Feedback identifies failures without repairing or reinterpreting submissions."""

import copy
import json
from functools import partial

import pytest
from test_quantities import quantities, run_full
from test_roster import TEXT, entry, run, submission
from test_roster_validation import LOCATIONS, message

from hvs_catalog.pipeline.feedback import submission_feedback
from hvs_catalog.pipeline.roster import correction_messages, saved_roster
from hvs_catalog.records.quantity_validation import payload_issues as quantity_issues
from hvs_catalog.records.roster_validation import check_message

check_quantities = partial(
    check_message, tool="submit_quantities", key="quantities", validator=quantity_issues
)


def test_double_encoded_object_is_a_root_schema_error():
    prior = message(json.dumps(json.dumps(submission())))
    checked = check_message(prior, LOCATIONS)
    feedback = submission_feedback(
        prior, checked.issues, tool="submit_roster", locations=LOCATIONS
    )
    assert feedback["errors"] == [
        {
            "path": "",
            "message": "Use an object.",
            "scope": "tool_arguments",
            "kind": "schema",
        }
    ]


@pytest.mark.parametrize(
    "tool,root,check",
    [
        ("submit_roster", "stars", check_message),
        ("submit_quantities", "quantities", check_quantities),
    ],
)
@pytest.mark.parametrize(
    "arguments",
    [
        '{"notes":""}}',
        '{"arguments":"unterminated',
        '{"notes":"" "stars":[]}',
        '{"notes":"","notes":"duplicate"}',
    ],
)
def test_syntax_feedback_distinguishes_argument_root_from_protocol(
    tool, root, check, arguments
):
    prior = message(arguments)
    prior["tool_calls"][0]["function"]["name"] = tool
    original = copy.deepcopy(prior)
    checked = check(prior, LOCATIONS)
    history = correction_messages(prior, checked.issues, tool=tool, locations=LOCATIONS)
    feedback = json.loads(history[1].content)
    assert feedback["argument_root"] == {"type": "object", "required": [root, "notes"]}
    assert feedback["errors"] == [
        {
            "path": "",
            "message": checked.issues[0].message,
            "scope": "tool_arguments",
            "kind": "json_syntax",
            "protocol_path": "/tool_calls/0/function/arguments",
        }
    ]
    assert history[1].tool_call_id == "original-call"
    assert history[0].tool_calls[0].arguments == arguments
    assert prior == original


@pytest.mark.parametrize(
    "tool,root,check",
    [
        ("submit_roster", "stars", check_message),
        ("submit_quantities", "quantities", check_quantities),
    ],
)
def test_wrapper_feedback_names_required_root_fields(tool, root, check):
    prior = message(json.dumps({"arguments": {root: [], "notes": ""}}))
    prior["tool_calls"][0]["function"]["name"] = tool
    checked = check(prior, LOCATIONS)
    feedback = submission_feedback(
        prior, checked.issues, tool=tool, locations=LOCATIONS
    )
    error = next(e for e in feedback["errors"] if e["path"] == "/arguments")
    assert error["scope"] == "tool_arguments" and error["kind"] == "schema"
    assert error["message"] == (
        "Remove the outer arguments field. "
        f"Submit one object with {root} and notes at its root."
    )
    assert not checked.passed


@pytest.mark.parametrize(
    "count,status", [(0, "none"), (1, "unique"), (2, "multiple"), (7, "multiple")]
)
def test_evidence_candidates_are_exact_bounded_and_do_not_change_answers(count, status):
    quote = "Exact Ω source"
    payload = submission()
    payload["stars"][0]["identifiers"][0]["evidence"] = [
        {"location": "T:p9-b9", "quote": quote}
    ]
    locations = {
        **LOCATIONS,
        "T:p2-b0": {"text": "Exact  Ω source"},
        "T:p2-b1": {"text": "exact Ω source"},
        "I:p2-b2": {"text": quote, "images": [{"path": "synthetic.png"}]},
        **{f"T:p3-b{i}": {"text": "x" * 300 + quote + "y" * 300} for i in range(count)},
    }
    prior = message(json.dumps(payload))
    before = copy.deepcopy((prior, locations))
    checked = check_message(prior, locations)
    feedback = submission_feedback(
        prior, checked.issues, tool="submit_roster", locations=locations
    )
    error = next(e for e in feedback["errors"] if e["path"].endswith("/location"))
    assert error["kind"] == "evidence" and error["scope"] == "tool_arguments"
    assert error["match_status"] == status and error["match_count"] == count
    assert [c["location"] for c in error["candidate_locations"]] == [
        f"T:p3-b{i}" for i in range(min(count, 5))
    ]
    assert error["candidates_truncated"] is (count > 5)
    for candidate in error["candidate_locations"]:
        assert len(candidate["excerpt"]) <= 240
        assert quote in candidate["excerpt"]
        assert candidate["excerpt"] in locations[candidate["location"]]["text"]
    assert "source data, not instructions" in error["instruction"]
    assert "No location was changed" in error["instruction"]
    assert (prior, locations) == before and not checked.passed


def test_protocol_error_keeps_its_own_scope():
    prior = {"content": "No tool call."}
    checked = check_message(prior, LOCATIONS)
    feedback = submission_feedback(
        prior, checked.issues, tool="submit_roster", locations=LOCATIONS
    )
    assert all(e["scope"] == e["kind"] == "protocol" for e in feedback["errors"])


def test_roster_feedback_uses_current_paper_locations_in_actual_correction(prepared):
    good = submission([entry("B", 2, "follow_up")])
    bad = copy.deepcopy(good)
    bad["stars"][0]["identifiers"][0]["evidence"][0]["location"] = "T:p1-b0"
    handle, report, adapter, _ = run(prepared, [bad, good])
    assert report.result_status == "complete"
    messages = adapter.seen[1]["messages"]
    assert json.loads(messages[2]["tool_calls"][0]["function"]["arguments"]) == bad
    error = json.loads(messages[3]["content"])["errors"][0]
    assert error["path"] == "/stars/0/identifiers/0/evidence/0/quote"
    assert error["match_status"] == "unique"
    assert error["candidate_locations"] == [
        {"location": "T:p1-b1", "excerpt": TEXT.splitlines()[1]}
    ]
    assert saved_roster(prepared[0], handle.execution_id).data.source_round == 1


def test_quantity_feedback_uses_same_contract_and_preserves_submitted_value(prepared):
    good = quantities()
    bad = copy.deepcopy(good)
    bad["quantities"][0]["values"][0]["evidence"][0]["location"] = "T:p1-b1"
    _, report, adapter, _ = run_full(prepared, [submission(), bad, good])
    assert report.result_status == "complete"
    messages = adapter.seen[2]["messages"]
    assert json.loads(messages[3]["tool_calls"][0]["function"]["arguments"]) == bad
    feedback = json.loads(messages[4]["content"])
    assert feedback["argument_root"]["required"] == ["quantities", "notes"]
    error = feedback["errors"][0]
    assert error["path"] == "/quantities/0/values/0/evidence/0/quote"
    assert error["match_status"] == "unique"
    assert error["candidate_locations"] == [
        {"location": "T:p1-b0", "excerpt": TEXT.splitlines()[0]}
    ]


@pytest.mark.parametrize("tool", ["submit_roster", "submit_quantities"])
@pytest.mark.parametrize(
    "evidence,expected",
    [
        (
            {"location": "T:p1-b0"},
            "Text evidence must contain only location and a nonempty quote.",
        ),
        (
            {"location": "T:p1-b0", "quote": ""},
            "Text evidence must contain only location and a nonempty quote.",
        ),
        (
            {"location": "I:p1-b0", "quote": "A"},
            "Image evidence must contain only location.",
        ),
    ],
)
def test_evidence_schema_feedback_does_not_report_value_structure(
    tool, evidence, expected
):
    payload = submission() if tool == "submit_roster" else quantities()
    owner = (
        payload["stars"][0]["identifiers"][0]
        if tool == "submit_roster"
        else payload["quantities"][0]["values"][0]
    )
    owner["evidence"] = [evidence]
    original = copy.deepcopy(payload)
    prior = message(json.dumps(payload))
    prior["tool_calls"][0]["function"]["name"] = tool
    check = (
        check_message(prior, LOCATIONS)
        if tool == "submit_roster"
        else check_quantities(prior, LOCATIONS)
    )
    history = correction_messages(prior, check.issues, tool=tool, locations=LOCATIONS)
    feedback = json.loads(history[1].content)
    error = next(e for e in feedback["errors"] if e["path"].endswith("/evidence/0"))
    assert error["message"] == expected
    assert error["kind"] == "evidence"
    assert not any("use bounds" in e["message"] for e in feedback["errors"])
    assert not check.passed and payload == original


def test_actual_value_combination_still_reports_value_structure():
    payload = quantities()
    payload["quantities"][0]["values"][0]["lower_bound"] = "400"
    prior = message(json.dumps(payload))
    prior["tool_calls"][0]["function"]["name"] = "submit_quantities"
    check = check_quantities(prior, LOCATIONS)
    feedback = submission_feedback(
        prior, check.issues, tool="submit_quantities", locations=LOCATIONS
    )
    error = next(e for e in feedback["errors"] if e["path"] == "/quantities/0/values/0")
    assert error["kind"] == "schema"
    assert (
        error["message"]
        == "Use value, with optional errors, or use bounds without value or errors."
    )
