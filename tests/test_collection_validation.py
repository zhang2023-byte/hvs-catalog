"""集合校验：引用损坏、论文错配、材料哈希与定位核对必须报错。"""

from __future__ import annotations

import shutil

import pytest
from records_helpers import (
    FIXTURES,
    fixture_by_name,
    load_fixture,
    refresh_release_manifest,
)

from hvs_catalog.records import (
    CollectionValidationError,
    parse_record,
    parse_records,
    records_to_json,
    validate_collection,
)


def as_payload(record):
    return record.model_dump(mode="json", by_alias=True)


def replace(records, record_id, transform):
    changed = []
    for record in records:
        if record.record_id == record_id:
            payload = as_payload(record)
            transform(payload)
            if payload["schema"]["name"] == "release":
                # 清单哈希按当前内容重算，避免篡改被哈希检查先行拦截
                refresh_release_manifest(payload)
            changed.append(parse_record(payload))
        else:
            changed.append(record)
    return changed


def test_fixtures_valid_with_materials():
    for name in ("gap-conflict", "numeric-forms"):
        validate_collection(load_fixture(name), materials_root=FIXTURES)


def test_missing_reference_target_rejected():
    records = load_fixture("gap-conflict")
    trimmed = [r for r in records if r.record_id != "gap-conflict-roster"]
    with pytest.raises(CollectionValidationError, match="引用的记录不存在"):
        validate_collection(trimmed)


def test_stale_revision_reference_rejected():
    records = load_fixture("gap-conflict")

    def bump(payload):
        payload["data"]["roster_ref"]["revision"] = 9

    with pytest.raises(CollectionValidationError, match="引用的记录不存在"):
        validate_collection(replace(records, "gap-conflict-paper", bump))


def test_duplicate_revision_rejected():
    records = load_fixture("gap-conflict")
    duplicated = records + [records[0]]
    with pytest.raises(CollectionValidationError, match="修订 1 重复保存"):
        validate_collection(duplicated)


def test_paper_mismatch_rejected():
    records = load_fixture("gap-conflict")

    def other_paper(payload):
        payload["data"]["paper_key"] = "SYNTHETIC-OTHER"

    with pytest.raises(CollectionValidationError, match="paper_key"):
        validate_collection(replace(records, "gap-conflict-roster", other_paper))


def test_evidence_of_other_paper_rejected():
    records = load_fixture("gap-conflict")

    def other_paper(payload):
        payload["data"]["paper_key"] = "SYNTHETIC-OTHER"

    with pytest.raises(CollectionValidationError, match="paper_key"):
        validate_collection(replace(records, "gap-conflict-ev-values", other_paper))


def test_evidence_file_outside_source_rejected():
    records = load_fixture("gap-conflict")

    def wrong_file(payload):
        payload["data"]["file_id"] = "fictional-file"

    with pytest.raises(CollectionValidationError, match="不在来源包中"):
        validate_collection(replace(records, "gap-conflict-ev-values", wrong_file))


def test_roster_binding_broken_rejected():
    records = load_fixture("gap-conflict")

    def wrong_contribution(payload):
        payload["data"]["entries"][0]["contribution_id"] = "contribution-fictional"

    with pytest.raises(CollectionValidationError, match="一一对应"):
        validate_collection(replace(records, "gap-conflict-roster", wrong_contribution))


def test_object_link_to_missing_item_rejected():
    records = load_fixture("gap-conflict")

    def wrong_item(payload):
        payload["data"]["contribution_links"][0]["item_id"] = "contribution-fictional"

    with pytest.raises(CollectionValidationError, match="不在论文"):
        validate_collection(replace(records, "gap-conflict-object", wrong_item))


def test_decision_fingerprint_binding():
    records = load_fixture("gap-conflict")

    def other_fingerprint(payload):
        payload["data"]["fingerprint"] = "f" * 64

    with pytest.raises(CollectionValidationError, match="指纹"):
        validate_collection(
            replace(records, "gap-conflict-question", other_fingerprint)
        )


def test_release_must_reference_run_record():
    records = load_fixture("gap-conflict")

    def wrong_target(payload):
        payload["data"]["source_run_refs"][0]["record_id"] = "gap-conflict-paper"

    with pytest.raises(CollectionValidationError, match="应指向 run"):
        validate_collection(replace(records, "gap-conflict-release", wrong_target))


def test_material_hash_change_rejected(tmp_path):
    records = load_fixture("gap-conflict")
    shutil.copytree(FIXTURES / "materials", tmp_path / "materials")
    target = tmp_path / "materials" / "gap-conflict" / "table.csv"
    target.write_text(target.read_text(encoding="utf-8").replace("525", "526"))
    with pytest.raises(CollectionValidationError, match="哈希改变"):
        validate_collection(records, materials_root=tmp_path)


def test_text_locator_out_of_range_rejected():
    records = load_fixture("gap-conflict")

    def out_of_range(payload):
        payload["data"]["locator"]["end_line"] = 99

    with pytest.raises(CollectionValidationError, match="行范围越界"):
        validate_collection(
            replace(records, "gap-conflict-ev-values", out_of_range),
            materials_root=FIXTURES,
        )


def test_text_excerpt_mismatch_rejected():
    records = load_fixture("gap-conflict")

    def rewritten(payload):
        payload["data"]["excerpt"] = (
            "A paraphrased sentence that is not the source text."
        )

    with pytest.raises(CollectionValidationError, match="引文与指定版本"):
        validate_collection(
            replace(records, "gap-conflict-ev-values", rewritten),
            materials_root=FIXTURES,
        )


def test_table_cell_column_missing_rejected():
    records = load_fixture("gap-conflict")

    def wrong_column(payload):
        payload["data"]["locator"]["column"] = "does_not_exist"

    with pytest.raises(CollectionValidationError, match="表格列不存在"):
        validate_collection(
            replace(records, "gap-conflict-ev-table-value", wrong_column),
            materials_root=FIXTURES,
        )


def test_table_cell_row_key_mismatch_rejected():
    records = load_fixture("gap-conflict")

    def wrong_row(payload):
        payload["data"]["locator"]["row_key"] = {"object": "FIC-B"}

    with pytest.raises(CollectionValidationError, match="行键定位不到对象行"):
        validate_collection(
            replace(records, "gap-conflict-ev-table-value", wrong_row),
            materials_root=FIXTURES,
        )


def test_json_pointer_mismatch_rejected():
    records = load_fixture("numeric-forms")

    def wrong_pointer(payload):
        payload["data"]["locator"]["selector"]["value"] = "/does_not_exist"

    with pytest.raises(CollectionValidationError, match="JSON 定位"):
        validate_collection(
            replace(records, "numeric-forms-ev-parallax", wrong_pointer),
            materials_root=FIXTURES,
        )


def test_fragment_outside_excerpt_rejected():
    records = load_fixture("gap-conflict")
    paper = fixture_by_name(records, "paper")
    payload = as_payload(paper)
    value = payload["data"]["object_contributions"][0]["quantities"][0]["values"][0]
    # 数值与片段保持一致，模型检查通过；片段不在任何原文里，集合检查必须报错
    value["value"] = "9999"
    value["direct_evidence"][0]["raw_fragment"] = "9999"
    tampered = [
        parse_record(payload) if r.record_id == paper.record_id else r for r in records
    ]
    with pytest.raises(CollectionValidationError, match="不在证据"):
        validate_collection(tampered)


def test_external_raw_response_hash_checked(tmp_path):
    records = load_fixture("gap-conflict")
    shutil.copytree(FIXTURES / "materials", tmp_path / "materials")
    target = tmp_path / "materials" / "gap-conflict" / "external-response.json"
    target.write_text("{}")
    with pytest.raises(CollectionValidationError, match="哈希改变"):
        validate_collection(records, materials_root=tmp_path)


def test_summary_and_type_evidence_separately_traceable():
    records = load_fixture("gap-conflict")
    paper = fixture_by_name(records, "paper")
    evidence = {r.record_id: r for r in records if r.RECORD_NAME == "evidence"}
    contribution = paper.data.object_contributions[0]
    summary_refs = [
        ref.record_id for ref in contribution.contribution_summary.evidence_refs
    ]
    type_refs = [ref.record_id for ref in contribution.contribution_type.evidence_refs]
    assert summary_refs and type_refs
    for record_id in set(summary_refs + type_refs):
        assert record_id in evidence
        assert evidence[record_id].data.paper_key == paper.data.paper_key


def test_collection_json_round_trip_after_validation():
    records = load_fixture("gap-conflict")
    validate_collection(records, materials_root=FIXTURES)
    payload = records_to_json(records)
    again = parse_records(payload)
    validate_collection(again, materials_root=FIXTURES)
    assert again == records
