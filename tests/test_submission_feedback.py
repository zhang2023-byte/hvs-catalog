"""Generic feedback reports validator facts without case-specific repair instructions."""

import copy
import json
from functools import partial

import pytest
from test_quantities import quantities, run_full
from test_roster import submission
from test_roster_validation import LOCATIONS, message

from hvs_catalog.pipeline.feedback import submission_feedback
from hvs_catalog.pipeline.roster import correction_messages
from hvs_catalog.records.quantity_validation import payload_issues as quantity_issues
from hvs_catalog.records.roster_validation import check_message

check_quantities = partial(
    check_message, tool="submit_quantities", key="quantities", validator=quantity_issues
)


def test_double_encoded_object_uses_the_same_type_constraint():
    prior = message(json.dumps(json.dumps(submission())))
    checked = check_message(prior, LOCATIONS)
    feedback = submission_feedback(prior, checked.issues, tool="submit_roster")
    assert feedback["errors"] == [
        {"kind": "schema", "path": "", "rule": "type", "constraint": "object"}
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
        r'{"notes":"520 \pm 10"}',
        '{"notes":""}}',
        '{"notes":"unterminated',
        '{"notes":"" "stars":[]}',
        '{"notes":"","notes":"duplicate"}',
        '{"notes":NaN}',
    ],
)
def test_syntax_uses_parser_details_without_protocol_paths(
    tool, root, check, arguments
):
    prior = message(arguments)
    prior["tool_calls"][0]["function"]["name"] = tool
    original = copy.deepcopy(prior)
    checked = check(prior, LOCATIONS)
    history = correction_messages(prior, checked.issues, tool=tool)
    feedback = json.loads(history[1].content)
    assert len(feedback["errors"]) == 1
    error = feedback["errors"][0]
    assert error["kind"] == "json_syntax"
    assert not {"protocol_path", "scope", "path"} & error.keys()
    if "Duplicate" in error["message"] or "Non-finite" in error["message"]:
        assert "line" not in error
    else:
        assert error["line"] == 1 and error["column"] > 0
        assert error["excerpt"] in arguments
    assert root + " and notes" in history[-1].content
    assert "/tool_calls" not in history[-1].content
    assert history[0].tool_calls[0].arguments == arguments
    assert history[1].tool_call_id == "original-call"
    assert prior == original


@pytest.mark.parametrize(
    "wrapper", ["arguments", "argument", "result", "unfamiliar_name"]
)
@pytest.mark.parametrize(
    "tool,root,check",
    [
        ("submit_roster", "stars", check_message),
        ("submit_quantities", "quantities", check_quantities),
    ],
)
def test_all_wrappers_use_generic_object_constraints(wrapper, tool, root, check):
    prior = message(json.dumps({wrapper: {root: [], "notes": ""}}))
    prior["tool_calls"][0]["function"]["name"] = tool
    checked = check(prior, LOCATIONS)
    assert submission_feedback(prior, checked.issues, tool=tool)["errors"] == [
        {
            "kind": "schema",
            "path": "",
            "missing": [root, "notes"],
            "unexpected": [wrapper],
        }
    ]
    assert not checked.passed


def test_nested_fields_use_the_same_constraints_without_echoing_content():
    payload = quantities()
    payload["quantities"][0].update(payload["quantities"][0].pop("values")[0])
    prior = message(json.dumps(payload))
    prior["tool_calls"][0]["function"]["name"] = "submit_quantities"
    check = check_quantities(prior, LOCATIONS)
    feedback = submission_feedback(prior, check.issues, tool="submit_quantities")
    assert feedback["errors"] == [
        {
            "kind": "schema",
            "path": "/quantities/0",
            "missing": ["values"],
            "unexpected": ["evidence", "unit", "value"],
        }
    ]
    assert "520" not in json.dumps(feedback)


def test_unknown_location_reports_fact_without_candidate_search():
    payload = submission()
    payload["stars"][0]["identifiers"][0]["evidence"][0]["location"] = "T:p9-b9"
    prior = message(json.dumps(payload))
    check = check_message(prior, LOCATIONS, quote_warnings=True)
    errors = submission_feedback(prior, check.issues, tool="submit_roster")["errors"]
    assert len(errors) == 2
    assert all(e["path"].endswith("/location") for e in errors)
    assert "T:p9-b9" in errors[0]["message"]
    assert "candidate_locations" not in errors[0]
    assert not check.passed


def test_protocol_errors_report_required_call_without_internal_paths():
    prior = {"content": "No tool call."}
    checked = check_message(prior, LOCATIONS)
    history = correction_messages(prior, checked.issues)
    feedback = submission_feedback(prior, checked.issues, tool="submit_roster")
    assert feedback["errors"][0]["received_calls"] == 0
    assert feedback["errors"][0]["expected_tool"] == "submit_roster"
    assert '"errors"' in history[-1].content


def test_actual_quantity_correction_preserves_raw_history(prepared):
    good = quantities()
    bad = copy.deepcopy(good)
    bad["quantities"][0]["values"][0]["evidence"][0]["location"] = "T:p9-b9"
    _, report, adapter, _ = run_full(prepared, [submission(), bad, good])
    assert report.result_status == "complete", report.errors
    messages = adapter.seen[2]["messages"]
    assert json.loads(messages[3]["tool_calls"][0]["function"]["arguments"]) == bad
    feedback = json.loads(messages[4]["content"])
    assert feedback["errors"][0]["path"] == "/quantities/0/values/0/evidence/0/location"
    assert "quantities and notes" in messages[5]["content"]


@pytest.mark.parametrize("tool", ["submit_roster", "submit_quantities"])
@pytest.mark.parametrize(
    "evidence",
    [
        {"location": "T:p1-b0"},
        {"location": "T:p1-b0", "quote": ""},
        {"location": "I:p1-b0", "quote": "A"},
    ],
)
def test_evidence_shapes_report_schema_constraints_without_numerical_advice(
    tool, evidence
):
    payload = submission() if tool == "submit_roster" else quantities()
    owner = (
        payload["stars"][0]["identifiers"][0]
        if tool == "submit_roster"
        else payload["quantities"][0]["values"][0]
    )
    owner["evidence"] = [evidence]
    prior = message(json.dumps(payload))
    prior["tool_calls"][0]["function"]["name"] = tool
    check = (
        check_message(prior, LOCATIONS)
        if tool == "submit_roster"
        else check_quantities(prior, LOCATIONS)
    )
    errors = submission_feedback(prior, check.issues, tool=tool)["errors"]
    error = next(e for e in errors if e.get("path", "").endswith("/evidence/0"))
    assert error["rule"] == "oneOf" and error["kind"] == "schema"
    assert "lower_bound" not in json.dumps(error)
    assert not check.passed
