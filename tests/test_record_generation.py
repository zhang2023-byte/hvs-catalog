"""生成说明与共同定义一致：产物同步、内容完整、管理字段不泄露。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from hvs_catalog.records import RECORD_TYPE_NAMES, load_rules, quantity_paths
from hvs_catalog.records.generate import (
    COMMON_INSTRUCTIONS,
    build_outputs,
    check_outputs,
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
    for key in ("submission.roster", "submission.candidate", "submission.quantity"):
        text = outputs[f"schemas/{key}.json"]
        schema = json.loads(text)
        # 管理字段不出现在任何层级
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
    assert doc.count("#### ") >= 40  # 各模型字段表齐全
    assert "Cross-record rules" in doc


def test_quantity_template_lists_scope_and_instructions():
    outputs = build_outputs()
    template = outputs["templates/quantity-extraction.md"]
    for path in quantity_paths():
        assert f"`{path}`" in template
    assert COMMON_INSTRUCTIONS in template
    assert "numeric_conflict" in template


def test_all_templates_share_common_instructions():
    outputs = build_outputs()
    for name in ("roster-extraction", "candidate-judgment", "quantity-extraction"):
        assert COMMON_INSTRUCTIONS in outputs[f"templates/{name}.md"]


def test_omittable_fields_derived_from_submission_model():
    outputs = build_outputs()
    doc = outputs["record-format.md"]
    # 简化填写允许省略的可空字段在说明中明确列出
    assert "`error_components`" in doc
    assert "interval_kind" in doc


def test_generation_is_deterministic():
    first = build_outputs()
    second = build_outputs()
    assert first == second
