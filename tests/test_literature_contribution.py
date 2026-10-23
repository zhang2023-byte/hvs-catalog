"""Final scientific documents preserve approved content and reject malformed shapes."""

from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from hvs_catalog.records import (
    RECORD_MODELS,
    LiteratureContribution,
    literature_contribution_to_json,
    load_literature_contribution,
    load_rules,
    parse_literature_contribution,
    save_literature_contribution,
)
from hvs_catalog.records.generate import build_outputs
from hvs_catalog.records.literature_contribution import LiteratureValue

FIXTURE = Path(__file__).parent / "fixtures" / "literature_contribution.json"
EVIDENCE = [{"location": "T:p3-b1", "quote": "A fictional reported measurement."}]
VALUE_SCHEMA = Draft202012Validator(LiteratureValue.model_json_schema())
DOCUMENT_SCHEMA = Draft202012Validator(LiteratureContribution.model_json_schema())


@pytest.fixture
def document():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def assert_invalid(document):
    assert list(DOCUMENT_SCHEMA.iter_errors(document))
    with pytest.raises(ValidationError):
        parse_literature_contribution(document)
    with pytest.raises(ValidationError):
        parse_literature_contribution(json.dumps(document))


def first_value(document):
    return document["stars"][0]["quantities"][0]["values"][0]


def test_approved_example_round_trip_and_generated_schema(document, tmp_path):
    schema = json.loads(build_outputs()["schemas/literature_contribution.json"])
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(document)
    parsed = load_literature_contribution(FIXTURE)
    assert parsed == parse_literature_contribution(document)
    text = literature_contribution_to_json(parsed)
    assert json.loads(text) == document
    assert list(json.loads(text)) == ["metadata", "stars", "notes"]
    assert "±" in text and "≤" in text and "7.0–9.0" in text
    assert text.endswith("\n")
    output = tmp_path / "new-directory" / "literature_contribution.json"
    save_literature_contribution(output, parsed)
    assert output.read_text(encoding="utf-8") == text
    assert load_literature_contribution(output) == parsed
    assert LiteratureContribution not in RECORD_MODELS.values()


@pytest.mark.parametrize(
    "value",
    [
        {"value": "-0.000"},
        {"value": "12:03:04.500", "unit": "h:m:s"},
        {"value": "≈ 1.20 × 10³", "error": "± 0.10 × 10³"},
        {"value": "8.20", "lower_error": "-0.60", "upper_error": "+0.80"},
        {"value": "8.20", "lower_error": "-0.60"},
        {"value": "8.20", "upper_error": "+0.80"},
        {"lower_bound": "5.0"},
        {"lower_bound": "5.0", "lower_inclusive": False},
        {"upper_bound": "10.0", "upper_inclusive": True},
        {"lower_bound": "7.0", "upper_bound": "9.0"},
        {
            "lower_bound": "7.0",
            "upper_bound": "9.0",
            "lower_inclusive": True,
            "upper_inclusive": False,
        },
        # Structural validation does not calculate expressions or order endpoints.
        {"lower_bound": "2 × R", "upper_bound": "R"},
    ],
)
def test_numeric_forms_preserve_original_components(document, value):
    value = {**value, "evidence": EVIDENCE}
    document["stars"][0]["quantities"][0]["values"] = [value]
    DOCUMENT_SCHEMA.validate(document)
    result = json.loads(
        literature_contribution_to_json(parse_literature_contribution(document))
    )
    assert first_value(result) == value
    assert "null" not in literature_contribution_to_json(
        parse_literature_contribution(document)
    )


def test_all_numeric_component_combinations_agree_with_schema():
    components = (
        "value",
        "error",
        "lower_error",
        "upper_error",
        "lower_bound",
        "upper_bound",
        "lower_inclusive",
        "upper_inclusive",
    )
    for mask in itertools.product((False, True), repeat=len(components)):
        fields = {key for key, include in zip(components, mask, strict=True) if include}
        payload = {key: False if key.endswith("inclusive") else "1.0" for key in fields}
        payload["evidence"] = EVIDENCE
        # Independently enumerate the allowed point and bound forms.
        point = fields & {"value", "lower_bound", "upper_bound"} == {"value"}
        bound = bool(fields & {"lower_bound", "upper_bound"}) and not fields & {
            "value",
            "error",
            "lower_error",
            "upper_error",
        }
        mixed_error = "error" in fields and bool(
            fields & {"lower_error", "upper_error"}
        )
        orphan_endpoint = any(
            f"{side}_inclusive" in fields and f"{side}_bound" not in fields
            for side in ("lower", "upper")
        )
        expected = (point or bound) and not mixed_error and not orphan_endpoint
        assert VALUE_SCHEMA.is_valid(payload) == expected, fields
        if expected:
            parsed = LiteratureValue.model_validate(payload)
            assert parsed.model_dump(mode="json") == payload
        else:
            with pytest.raises(ValidationError):
                LiteratureValue.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    [
        "value",
        "error",
        "lower_error",
        "upper_error",
        "lower_bound",
        "upper_bound",
        "lower_inclusive",
        "upper_inclusive",
        "unit",
        "condition",
    ],
)
def test_optional_null_is_rejected(document, field):
    first_value(document)[field] = None
    assert_invalid(document)


@pytest.mark.parametrize("value", [1, 1.2, True, "", [], {}])
def test_numeric_components_must_be_nonempty_strings(document, value):
    first_value(document)["value"] = value
    assert_invalid(document)


@pytest.mark.parametrize("inclusive", [0, 1, "true", "false", [], {}])
def test_inclusion_is_a_boolean_without_coercion(document, inclusive):
    document["stars"][0]["quantities"][0]["values"] = [
        {"lower_bound": "5.0", "lower_inclusive": inclusive, "evidence": EVIDENCE}
    ]
    assert_invalid(document)


@pytest.mark.parametrize(
    "path",
    [
        ("metadata",),
        ("stars",),
        ("notes",),
        ("metadata", "arxiv_id"),
        ("metadata", "version"),
        ("metadata", "title"),
        ("metadata", "authors"),
        ("stars", 0, "identifiers"),
        ("stars", 0, "contribution_type"),
        ("stars", 0, "quantities"),
        ("stars", 0, "identifiers", 0, "evidence"),
        ("stars", 0, "contribution_type", "evidence"),
        ("stars", 0, "quantities", 0, "quantity"),
        ("stars", 0, "quantities", 0, "values"),
        ("stars", 0, "quantities", 0, "values", 0, "evidence"),
        ("stars", 0, "quantities", 0, "values", 0, "evidence", 0, "location"),
        ("stars", 0, "quantities", 0, "values", 0, "evidence", 0, "quote"),
    ],
)
def test_required_fields(document, path):
    parent = document
    for key in path[:-1]:
        parent = parent[key]
    del parent[path[-1]]
    assert_invalid(document)


@pytest.mark.parametrize(
    "path",
    [
        ("stars", 0, "identifiers"),
        ("stars", 0, "identifiers", 0, "evidence"),
        ("stars", 0, "contribution_type", "evidence"),
        ("stars", 0, "quantities", 0, "values"),
        ("stars", 0, "quantities", 0, "values", 0, "evidence"),
    ],
)
def test_reported_items_require_content(document, path):
    parent = document
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = []
    assert_invalid(document)


@pytest.mark.parametrize(
    "path, field",
    [
        ((), "paper_key"),
        ((), "object_contributions"),
        ((), "extraction_notes"),
        ((), "calls"),
        ((), "status"),
        ((), "schema"),
        ((), "execution_id"),
        (("metadata",), "source"),
        (("stars", 0), "contribution_summary"),
        (("stars", 0), "paper_boundness"),
        (("stars", 0), "conditional_assessments"),
        (("stars", 0), "coverage"),
        (("stars", 0), "review_exclusions"),
        (("stars", 0), "author_errors"),
        (("stars", 0, "identifiers", 0), "source"),
        (("stars", 0, "contribution_type"), "source_note"),
        (("stars", 0, "quantities", 0), "source"),
        (("stars", 0, "quantities", 0, "values", 0), "frame"),
        (("stars", 0, "quantities", 0, "values", 0), "source"),
        (("stars", 0, "quantities", 0, "values", 0), "source_note"),
        (("stars", 0, "quantities", 0, "values", 0, "evidence", 0), "page"),
    ],
)
def test_removed_and_unknown_fields_are_rejected(document, path, field):
    parent = document
    for key in path:
        parent = parent[key]
    parent[field] = "obsolete"
    assert_invalid(document)


@pytest.mark.parametrize(
    "arxiv_id", ["2601.99999", "0704.0001", "astro-ph/0501234", "math.GT/0301234"]
)
def test_bare_arxiv_identifiers(document, arxiv_id):
    document["metadata"]["arxiv_id"] = arxiv_id
    DOCUMENT_SCHEMA.validate(document)
    assert parse_literature_contribution(document).metadata.arxiv_id == arxiv_id


@pytest.mark.parametrize(
    "field, value",
    [
        ("arxiv_id", "2601.99999v1"),
        ("arxiv_id", "SYNTHETIC-PAPER-001"),
        ("version", "1"),
        ("version", 1),
        ("version", "v0"),
        ("title", ""),
        ("authors", "A. Example"),
    ],
)
def test_malformed_metadata(document, field, value):
    document["metadata"][field] = value
    assert_invalid(document)


@pytest.mark.parametrize("value", ["kinematics.radial_velocity", "unknown"])
def test_quantity_uses_short_science_identifier(document, value):
    document["stars"][0]["quantities"][0]["quantity"] = value
    assert_invalid(document)


def test_all_existing_quantity_identifiers_are_supported(document):
    expected = [quantity.id for quantity in load_rules().quantities]
    quantities = []
    for quantity in expected:
        entry = copy.deepcopy(document["stars"][0]["quantities"][0])
        entry["quantity"] = quantity
        quantities.append(entry)
    document["stars"][0]["quantities"] = quantities
    DOCUMENT_SCHEMA.validate(document)
    result = parse_literature_contribution(document)
    assert [entry.quantity for entry in result.stars[0].quantities] == expected


@pytest.mark.parametrize("contribution_type", ["follow_up", "candidates_found"])
def test_contribution_types_and_empty_quantity_list(document, contribution_type):
    document["stars"][0]["contribution_type"]["value"] = contribution_type
    document["stars"][0]["quantities"] = []
    document["notes"] = []
    DOCUMENT_SCHEMA.validate(document)
    assert (
        json.loads(
            literature_contribution_to_json(parse_literature_contribution(document))
        )
        == document
    )


def test_no_stars_and_free_text_notes(document):
    document["stars"] = []
    document["notes"] = ["No qualifying stars were retained; see T:p2-b3."]
    DOCUMENT_SCHEMA.validate(document)
    assert parse_literature_contribution(document).stars == []


@pytest.mark.parametrize(
    "notes", [None, "", "A legacy note.", {}, 1, [None], [1], [""]]
)
def test_notes_requires_an_array_of_nonempty_strings(document, notes):
    document["notes"] = notes
    assert_invalid(document)


def test_note_order_and_text_survive_json_round_trip(document):
    notes = [
        "FIC-B was excluded. See T:p2-b3.",
        "FIC-A: A possible error affects the parallax. See T:p4-b2.",
    ]
    document["notes"] = notes
    DOCUMENT_SCHEMA.validate(document)
    parsed = parse_literature_contribution(document)
    assert parsed.notes == notes
    assert json.loads(literature_contribution_to_json(parsed))["notes"] == notes


def test_unknown_contribution_type(document):
    document["stars"][0]["contribution_type"]["value"] = "discovery"
    assert_invalid(document)


@pytest.mark.parametrize("payload", ["{", "[]", "null", "1", '"text"'])
def test_malformed_json_and_non_object_roots(payload):
    with pytest.raises(ValidationError):
        parse_literature_contribution(payload)


def test_file_errors_are_visible(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_literature_contribution(tmp_path / "missing.json")
    path = tmp_path / "invalid.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_literature_contribution(path)


def test_save_revalidates_mutable_lists_before_touching_file(document, tmp_path):
    parsed = parse_literature_contribution(document)
    parsed.stars[0].identifiers.clear()
    output = tmp_path / "literature_contribution.json"
    output.write_text("retained previous content", encoding="utf-8")
    with pytest.raises(ValidationError):
        save_literature_contribution(output, parsed)
    assert output.read_text(encoding="utf-8") == "retained previous content"


def test_value_assignment_validates_and_keeps_omitted_defaults():
    value = LiteratureValue(value="1.0", evidence=EVIDENCE)
    value.unit = "kpc"
    assert value.model_dump() == {"value": "1.0", "unit": "kpc", "evidence": EVIDENCE}
    with pytest.raises(ValidationError):
        value.unit = None
