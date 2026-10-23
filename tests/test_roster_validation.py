"""Independent negative cases for the new roster contract and exact evidence."""

import copy
import json

import pytest
from jsonschema import Draft202012Validator
from test_roster import entry, submission

from hvs_catalog.pipeline.roster import correction_messages
from hvs_catalog.records.roster_input import RosterSubmission, roster_input_schema
from hvs_catalog.records.roster_validation import (
    check_message,
    payload_issues,
    strict_json,
)

LOCATIONS = {
    "T:p1-b0": {"text": "A and Alias-A are one star. A is an unbound candidate."},
    "I:p1-b0": {"images": [{"path": "figure.png"}]},
}


def message(arguments):
    return {
        "tool_calls": [
            {
                "id": "original-call",
                "type": "function",
                "function": {"name": "submit_roster", "arguments": arguments},
            }
        ]
    }


@pytest.mark.parametrize(
    "location,quote",
    [
        ("p1-b0", "A"),
        ("T:p1-b0 (text)", "A"),
        ("LOCATION T:p1-b0", "A"),
        ("T:p1-b9", "A"),
        ("T:p0-b0", "A"),
        ("I:p1-b9", None),
        ("T:p1-b0", ""),
        ("T:p1-b0", "A  and Alias-A"),
        ("T:p1-b0", "A and Alias-A are the same star."),
        ("I:p1-b0", "A"),
    ],
)
def test_exact_locations_and_quotes(location, quote):
    payload = submission()
    evidence = {"location": location}
    if quote is not None:
        evidence["quote"] = quote
    payload["stars"][0]["identifiers"][0]["evidence"] = [evidence]
    original = copy.deepcopy(payload)
    assert payload_issues(payload, LOCATIONS)
    assert payload == original


@pytest.mark.parametrize(
    "path,value",
    [
        (("stars",), None),
        (("notes",), None),
        (("notes",), 1),
        (("stars", 0, "identifiers"), []),
        (("stars", 0, "identifiers"), ["A"]),
        (("stars", 0, "identifiers", 0, "value"), 42),
        (("stars", 0, "identifiers", 0, "value"), ""),
        (("stars", 0, "identifiers", 0, "evidence"), []),
        (("stars", 0, "contribution_type"), "follow_up"),
        (("stars", 0, "contribution_type", "value"), "discovery"),
        (("stars", 0, "contribution_type", "evidence"), []),
    ],
)
def test_schema_and_python_reject_same_shapes(path, value):
    payload = submission()
    item = payload
    for key in path[:-1]:
        item = item[key]
    item[path[-1]] = value
    assert not Draft202012Validator(roster_input_schema()).is_valid(payload)
    with pytest.raises(ValueError):
        RosterSubmission.model_validate(payload)
    assert payload_issues(payload, LOCATIONS)


@pytest.mark.parametrize(
    "path",
    [
        (),
        ("stars", 0),
        ("stars", 0, "identifiers", 0),
        ("stars", 0, "contribution_type"),
        ("stars", 0, "identifiers", 0, "evidence", 0),
    ],
)
def test_every_object_forbids_extra_fields(path):
    payload = submission()
    item = payload
    for key in path:
        item = item[key]
    item["extra"] = True
    assert payload_issues(payload, LOCATIONS)


def test_json_decoding_is_the_only_quote_transformation():
    payload = submission()
    payload["stars"][0]["identifiers"][0]["evidence"][0]["quote"] = "A"
    wire = json.dumps(payload).replace('"quote": "A"', '"quote": "\\u0041"')
    checked = check_message(message(wire), LOCATIONS)
    assert checked.passed
    assert checked.payload == payload
    for raw in ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '{"a":1e999}']:
        with pytest.raises(ValueError):
            strict_json(raw)


def test_plain_image_and_caption_are_separate_evidence_items():
    payload = submission()
    payload["stars"][0]["identifiers"][0]["evidence"] = [
        {"location": "I:p1-b0"},
        {"location": "T:p1-b0", "quote": "A"},
    ]
    assert not payload_issues(payload, LOCATIONS)
    assert RosterSubmission.model_validate(payload).model_dump() == payload


def test_exact_duplicate_does_not_guess_similar_identifiers():
    payload = submission([entry("A"), entry("a"), entry("A ")])
    assert not payload_issues(payload, LOCATIONS)
    payload["stars"][1]["identifiers"][0]["value"] = "A"
    issues = payload_issues(payload, LOCATIONS)
    assert {i.path for i in issues} == {"/stars/0/identifiers", "/stars/1/identifiers"}


@pytest.mark.parametrize("kind", ["missing", "multiple", "wrong", "duplicate_ids"])
def test_protocol_feedback_uses_only_real_call_ids(kind):
    prior = message(json.dumps(submission()))
    if kind == "missing":
        prior["tool_calls"][0].pop("id")
    if kind in {"multiple", "duplicate_ids"}:
        other = copy.deepcopy(prior["tool_calls"][0])
        other["id"] = "other" if kind == "multiple" else "original-call"
        prior["tool_calls"].append(other)
    if kind == "wrong":
        prior["tool_calls"][0]["function"]["name"] = "wrong"
    checked = check_message(prior, LOCATIONS)
    assert not checked.passed
    history = correction_messages(prior, checked.issues)
    tools = [m for m in history if m.role == "tool"]
    assert [m.tool_call_id for m in tools] == (
        ["original-call", "other"]
        if kind == "multiple"
        else ["original-call"]
        if kind == "wrong"
        else []
    )


@pytest.mark.parametrize("content", [None, 42, {"invalid": "content"}, []])
def test_partial_protocol_history_answers_each_associable_tool_call(content):
    prior = message(json.dumps(submission()))
    prior["content"] = content
    prior["tool_calls"].append(
        {"function": {"name": "submit_roster", "arguments": "{}"}}
    )
    checked = check_message(prior, LOCATIONS)
    history = correction_messages(prior, checked.issues)
    assert [m.role for m in history] == ["assistant", "tool", "user"]
    assert history[1].tool_call_id == "original-call"
    assert '"errors"' in history[1].content
    assert '"errors"' not in history[2].content
    assert '"function"' in history[0].content


def test_schema_feedback_uses_field_paths_without_echoing_full_answers():
    payload = submission()
    del payload["notes"]
    payload["stars"][0]["identifiers"][0]["evidence"][0]["quote"] = 42
    payload["extra"] = "Do not repeat this source text. " * 1000
    issues = payload_issues(payload, LOCATIONS)
    assert any(
        i.path == "/notes" and i.message == "Include this required field."
        for i in issues
    )
    assert any(
        i.path == "/extra" and i.message.startswith("Remove this field.")
        for i in issues
    )
    assert any(
        i.path.endswith("/evidence/0") and "nonempty quote" in i.message for i in issues
    )
    assert all("Do not repeat this source text" not in i.message for i in issues)
