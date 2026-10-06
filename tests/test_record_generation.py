"""Generated documentation matches shared definitions: synchronized, complete, and free of
administrative fields.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from hvs_catalog.records import RECORD_TYPE_NAMES, load_rules, quantity_paths
from hvs_catalog.records.generate import (
    COMMON_INSTRUCTIONS,
    build_outputs,
    check_outputs,
)
from hvs_catalog.records.submission import (
    ObjectQuantitySubmission,
    QuantitySubmission,
    RosterSubmission,
    ValueSubmission,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = REPO_ROOT / "contracts" / "generated"


def test_generated_files_in_sync():
    assert check_outputs(CONTRACTS) == [], check_outputs(CONTRACTS)


def test_tampered_output_detected(tmp_path):
    shutil.copytree(CONTRACTS, tmp_path / "generated")
    target = tmp_path / "generated" / "record-format.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nhand edit\n")
    assert any("过期" in problem for problem in check_outputs(tmp_path / "generated"))
    (tmp_path / "generated" / "schemas" / "extra.json").write_text("{}")
    assert any("多余" in problem for problem in check_outputs(tmp_path / "generated"))


def test_every_record_type_has_schema_and_doc_entry():
    outputs = build_outputs()
    for name in RECORD_TYPE_NAMES:
        schema = json.loads(outputs[f"schemas/record.{name}.json"])
        properties = schema["properties"]
        assert set(properties) == {
            "schema",
            "record_id",
            "revision",
            "created_at",
            "execution_id",
            "data",
        }
        assert schema["required"] == [
            "schema",
            "record_id",
            "revision",
            "created_at",
            "execution_id",
            "data",
        ]
        assert f"`{name}`" in outputs["record-format.md"]


def test_submission_schemas_do_not_leak_management_fields():
    outputs = build_outputs()
    for key in (
        "submission.roster",
        "submission.quantity",
    ):
        text = outputs[f"schemas/{key}.json"]
        schema = json.loads(text)
        # Administrative fields appear at no nesting level.
        for forbidden in (
            "record_id",
            "revision",
            "created_at",
            "execution_id",
            "schema",
        ):
            assert f'"{forbidden}"' not in text, (key, forbidden)
        assert schema.get("additionalProperties") is False


def test_format_document_covers_quantities_states_and_rules():
    outputs = build_outputs()
    doc = outputs["record-format.md"]
    for path in quantity_paths():
        assert f"`{path}`" in doc
    for state in load_rules().state_ids:
        assert f"`{state}`" in doc
    assert doc.count("#### ") >= 40  # Every model's field table is complete.
    assert "Cross-record rules" in doc


def test_quantity_template_lists_scope_and_instructions():
    outputs = build_outputs()
    template = outputs["templates/quantity-extraction.md"]
    for path in quantity_paths():
        assert f"`{path}`" in template
    assert COMMON_INSTRUCTIONS in template
    assert "Passive conflict diagnostics" in template
    assert "numeric_conflict" not in template
    assert "author_errors" in template


def test_all_templates_share_common_instructions():
    outputs = build_outputs()
    for name in (
        "roster-extraction",
        "quantity-extraction",
    ):
        assert COMMON_INSTRUCTIONS in outputs[f"templates/{name}.md"]


def test_omittable_fields_derived_from_submission_model():
    doc = build_outputs()["record-format.md"]
    required_line = next(
        line for line in doc.splitlines() if line.startswith("- Always provided")
    )
    optional_line = next(
        line for line in doc.splitlines() if line.startswith("- Omittable")
    )
    required = set(ValueSubmission.model_json_schema()["required"])
    assert required == {"limit_kind", "condition", "source", "evidence"}
    assert set(re.findall(r"`([^`]+)`", required_line)) == required
    # The explanation also quotes null; it is not a field name.
    optional = set(re.findall(r"`([^`]+)`", optional_line)) - {"null"}
    assert optional == set(ValueSubmission.model_fields) - required
    assert {"error_components", "interval_kind"} <= optional
    assert required.isdisjoint(optional)


def test_generated_tables_keep_one_column_per_header():
    doc = build_outputs()["record-format.md"]
    assert (
        "| `origin` | `SyntheticOrigin` \\| `ArxivOrigin` \\| `LocalPdfOrigin` | yes |"
        in doc
    )
    assert "| `kind` | `arxiv` | yes |" in doc
    columns = None
    for line in doc.splitlines():
        if not line.startswith("|"):
            columns = None
            continue
        count = len(re.findall(r"(?<!\\)\|", line))
        if columns is None:
            columns = count
        assert count == columns, line


def test_quantity_template_field_levels_match_submission_schema():
    from hvs_catalog.records.quantity_input import quantity_input_schema

    template = build_outputs()["templates/quantity-extraction.md"]
    assert QuantitySubmission.model_json_schema()["required"] == ["objects"]
    assert "questions" not in ObjectQuantitySubmission.model_fields
    coverage = ObjectQuantitySubmission.model_json_schema()["properties"]["coverage"]
    assert coverage == {"$ref": "#/$defs/CoverageDeclaration"}
    wire = quantity_input_schema()
    assert wire["required"] == [
        "measurements",
        "quantity_conflicts",
        "coverage",
    ]
    assert wire["properties"]["coverage"] == {"$ref": "#/$defs/CoverageDeclaration"}
    assert "Exactly one application-supplied object" in template
    assert "Do not submit objects, object_id" in template
    assert "a `coverage` declaration object" in template


def test_common_instructions_are_step_independent():
    # Roster and candidate submissions cannot contain quantity coverage declarations.
    assert "quantities" not in RosterSubmission.model_fields
    assert "all requested quantities" not in COMMON_INSTRUCTIONS
    assert "common coverage" not in COMMON_INSTRUCTIONS
    quantity = build_outputs()["templates/quantity-extraction.md"]
    assert "Declare the common state for all requested quantities" in quantity
    assert "never omit a reported component" not in COMMON_INSTRUCTIONS
    assert "exactly when the paper reports them" in quantity


def test_record_contract_docs_are_english():
    outputs = build_outputs()
    for name, text in outputs.items():
        if name.endswith(".md"):
            assert re.search(r"[\u4e00-\u9fff]", text) is None, name
    assert "retain their original form" in outputs["record-format.md"]


def test_generation_is_deterministic():
    first = build_outputs()
    second = build_outputs()
    assert first == second
