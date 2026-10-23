"""Approved prompt responsibilities and submission shapes stay independent of execution."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from hvs_catalog.llm.prompts import (
    PromptBlueprint,
    PromptInput,
    assemble_blueprint,
    science_content,
)
from hvs_catalog.pipeline import ExtractionConfig
from hvs_catalog.records.quantity_input import QuantitySubmission, quantity_input_schema
from hvs_catalog.records.roster_input import RosterSubmission


@pytest.mark.parametrize("stage", ["roster", "quantities"])
def test_system_keeps_science_and_materials_while_tools_describe_fields(stage):
    blueprint = assemble_blueprint(stage)
    system = blueprint.system
    assert [line for line in system.splitlines() if line.startswith("# ")] == [
        "# Task",
        "# Scientific rules",
        "# Materials",
        "# Submission",
    ]
    source = science_content()
    for rule in source["rules"]:
        if rule["id"] == "S03":
            if stage == "roster":
                assert rule["title"] not in system
            else:
                before, after = rule["text"].split("{{quantity_table}}")
                assert before in system and after in system
                for row in source["quantities"]:
                    assert row["definition"] in system
                    assert row["common_units"] in system
        elif rule["id"] == "S02" and stage == "quantities":
            assert rule["title"] not in system
        else:
            assert rule["text"] in system
    assert "{{quantity_table}}" not in system
    assert "Copy a nonempty excerpt" not in system
    assert "lower_inclusive" not in system
    assert "Use an empty string" not in system
    tool = blueprint.tool()
    assert tool.name == f"submit_{stage}"
    Draft202012Validator.check_schema(tool.parameters)
    assert "Copy a nonempty excerpt" in json.dumps(tool.parameters)
    assert (
        "Use an empty string" in tool.parameters["properties"]["notes"]["description"]
    )


@pytest.mark.parametrize(
    "stage,model", [("roster", RosterSubmission), ("quantities", QuantitySubmission)]
)
def test_tools_accept_final_scientific_fields_and_empty_submissions(stage, model):
    paper = json.loads(
        (Path(__file__).parent / "fixtures/literature_contribution.json").read_text()
    )
    if stage == "roster":
        field = "stars"
        items = [
            {key: star[key] for key in ("identifiers", "contribution_type")}
            for star in paper["stars"]
        ]
    else:
        field = "quantities"
        items = paper["stars"][0][field]
    validator = Draft202012Validator(assemble_blueprint(stage).schema_)
    for content in (items, []):
        payload = {field: content, "notes": ""}
        validator.validate(payload)
        assert model.model_validate(payload).model_dump(mode="json") == payload


@pytest.mark.parametrize(
    "payload",
    [
        {"measurements": [], "notes": ""},
        {"quantities": []},
        {"quantities": [], "notes": []},
        {"quantities": [{"quantity": "distance", "values": []}], "notes": ""},
    ],
)
def test_quantity_submission_rejects_other_shapes(payload):
    assert not Draft202012Validator(quantity_input_schema()).is_valid(payload)
    with pytest.raises(ValidationError):
        QuantitySubmission.model_validate(payload)


@pytest.mark.parametrize(
    "value",
    [
        {"value": 2},
        {"value": "2", "upper_bound": "3"},
        {"value": "2", "error": "1", "lower_error": "-1"},
        {"upper_bound": "3", "lower_inclusive": True},
    ],
)
def test_quantity_tool_preserves_final_value_constraints(value):
    value = {
        **value,
        "evidence": [{"location": "T:p1-b0", "quote": "Fictional result."}],
    }
    payload = {"quantities": [{"quantity": "distance", "values": [value]}], "notes": ""}
    assert not Draft202012Validator(quantity_input_schema()).is_valid(payload)
    with pytest.raises(ValidationError):
        QuantitySubmission.model_validate(payload)


def test_frozen_blueprint_keeps_its_tool_description():
    blueprint = assemble_blueprint("roster")
    payload = blueprint.model_dump(mode="json", by_alias=True)
    payload["tool_description"] = "A frozen tool description."
    restored = PromptBlueprint.model_validate(payload)
    request = restored.request(
        ExtractionConfig().default_model, PromptInput(manuscript="A")
    )
    assert request.tool.description == "A frozen tool description."
    del payload["tool_description"]
    with pytest.raises(ValidationError):
        PromptBlueprint.model_validate(payload)


def test_quantity_requests_require_target():
    blueprint = assemble_blueprint("quantities")
    assert "The first user message" in blueprint.system
    assert "The second user message" in blueprint.system
    with pytest.raises(ValueError, match="target star identifiers"):
        blueprint.request(ExtractionConfig().default_model, PromptInput(manuscript="A"))
    assert ExtractionConfig().model_steps() == ("roster", "quantities")
    with pytest.raises(ValueError, match="Unknown extraction stage"):
        assemble_blueprint("other")
