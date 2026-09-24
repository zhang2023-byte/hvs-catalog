"""Gold 格式生成说明与有类型定义一致：产物同步、模板与 Schema 完整。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from hvs_catalog.evaluation.generate import (
    GOLD_OUTPUT_DIRNAME,
    build_outputs,
    check_outputs,
)
from hvs_catalog.records.generate import check_outputs as check_record_outputs
from hvs_catalog.records.rules import quantity_paths

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = REPO_ROOT / "contracts" / "generated"
GOLD_CONTRACTS = CONTRACTS / GOLD_OUTPUT_DIRNAME


def test_gold_generated_files_in_sync():
    assert check_outputs(GOLD_CONTRACTS) == [], check_outputs(GOLD_CONTRACTS)


def test_record_generation_tolerates_gold_subtree():
    # records 的检查不把 gold/ 子树当作多余文件
    assert check_record_outputs(CONTRACTS) == []


def test_gold_tampered_output_detected(tmp_path):
    shutil.copytree(GOLD_CONTRACTS, tmp_path / "gold")
    target = tmp_path / "gold" / "gold-format.md"
    target.write_text(target.read_text(encoding="utf-8") + "\nhand edit\n")
    assert any("过期" in problem for problem in check_outputs(tmp_path / "gold"))
    (tmp_path / "gold" / "extra.md").write_text("x")
    assert any("多余" in problem for problem in check_outputs(tmp_path / "gold"))


def test_gold_format_document_covers_models_and_vocab():
    outputs = build_outputs()
    doc = outputs["legacy/gold-format.md"]
    for name in (
        "GoldAnnotation",
        "GoldEvidence",
        "GoldValue",
        "GoldQuantityGroup",
        "GoldConflict",
        "GoldExclusion",
        "GoldBoundness",
        "GoldProvenance",
        "GoldPendingItem",
    ):
        assert name in doc
    for vocabulary in ("ordinary_range", "sexagesimal_colon", "distance_modulus"):
        assert vocabulary in doc
    assert "legacy reader scope" in doc.lower() or "Legacy reader scope" in doc
    assert "not_assessed" in doc


def test_gold_schemas_are_valid_json_with_strict_fields():
    outputs = build_outputs()
    for key in ("gold.annotation", "gold.legacy"):
        schema = json.loads(outputs[f"legacy/schemas/{key}.json"])
        assert schema.get("additionalProperties") is False
        assert "properties" in schema


def test_annotation_template_lists_quantities_and_instructions():
    outputs = build_outputs()
    template = outputs["legacy/templates/gold-annotation.md"]
    for path in quantity_paths():
        assert f"`{path}`" in template
    assert "evaluation.hvs_contribution_gold" in template
    assert "draft" in template


def test_gold_generation_is_deterministic():
    assert build_outputs() == build_outputs()
