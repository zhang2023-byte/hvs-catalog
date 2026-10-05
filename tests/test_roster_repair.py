"""Compact schemas and local repairs retain all unrelated scientific fields."""

import copy
import json

import pytest
from jsonschema import Draft202012Validator, ValidationError
from test_roster import entry, evidence, submission

from hvs_catalog.pipeline.roster_repair import (
    apply_repairs,
    repair_tool,
    schema_repairs,
)
from hvs_catalog.records.roster_input import bind_roster_input, roster_input_schema
from hvs_catalog.records.submission import RosterSubmission


def test_wire_schema_omits_technical_evidence_and_contribution_wrappers():
    schema = roster_input_schema()
    serialized = json.dumps(schema)
    for key in ('"parts"', '"locator"', '"source"', '"contribution"', '"page"'):
        assert key not in serialized
    assert set(schema["$defs"]["InlineEvidence"]["properties"]) == {"location", "quote"}
    Draft202012Validator(schema).validate(submission())
    locations = {
        "T:p1-b2": {
            "file_id": "raw",
            "locator": {"kind": "pdf", "page": 1, "block_id": "p1-b2"},
        }
    }
    bound = RosterSubmission.model_validate(bind_roster_input(submission(), locations))
    assert bound.entries[0].contribution.summary.text == "This paper studies A."
    assert bound.entries[0].contribution.boundness.status == "unbound"
    assert bound.entries[0].evidence[0].source.locator.page == 1
    assert bound.entries[0].evidence[0].parts == []


@pytest.mark.parametrize(
    "kind", ["missing", "unknown", "bad_enum", "missing_quote", "empty_evidence"]
)
def test_only_invalid_leaf_is_exposed_and_every_other_value_is_preserved(kind):
    baseline = submission()
    item = baseline["entries"][0]
    # Remove fixture aliases so only the selected leaf is affected.
    baseline = json.loads(json.dumps(baseline))
    item = baseline["entries"][0]
    if kind == "missing":
        del item["conditional_assessments"]
        path, value = ("entries", 0, "conditional_assessments"), []
    elif kind == "unknown":
        item["unknown"] = "extra"
        path, value = ("entries", 0, "unknown"), None
    elif kind == "bad_enum":
        item["boundness"] = "UNBOUND"
        path, value = ("entries", 0, "boundness"), "unbound"
    elif kind == "missing_quote":
        del item["evidence"][0]["quote"]
        path, value = ("entries", 0, "evidence", 0, "quote"), evidence(3)["quote"]
    else:
        item["boundness_evidence"] = []
        path, value = ("entries", 0, "boundness_evidence"), [evidence(3)]
    schema = roster_input_schema()
    fixes = schema_repairs(baseline, schema)
    assert [fix.path for fix in fixes] == [path]
    tool = repair_tool(fixes, schema)
    Draft202012Validator(tool.parameters).validate({"R1": value})
    with pytest.raises(ValidationError):
        Draft202012Validator(tool.parameters).validate(
            {"R1": value, "entries": [entry("B", 4, "unbound")]}
        )
    original = copy.deepcopy(baseline)
    updated = apply_repairs(baseline, fixes, {"R1": value})
    assert baseline == original
    expected = submission()
    assert updated == expected
    Draft202012Validator(schema).validate(updated)


def test_missing_disposition_does_not_authorize_removing_scientific_fields():
    payload = submission()
    del payload["entries"][0]["disposition"]
    fixes = schema_repairs(payload, roster_input_schema())
    assert [fix.path for fix in fixes] == [("entries", 0, "disposition")]


def test_unknown_location_cannot_be_guessed_during_binding():
    with pytest.raises(KeyError):
        bind_roster_input(submission(), {})
