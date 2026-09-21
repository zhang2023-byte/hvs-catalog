"""测试用的合成记录构造与夹具装载；全部材料为虚构，不涉及 Gold 或真实服务。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from hvs_catalog.records import (
    MeasuredValue,
    ObjectContribution,
    PaperBoundness,
    QuantityConflict,
    QuantityCoverageEntry,
    RecordRef,
    SourcePackageRecord,
    build_record,
    parse_record,
    quantity_paths,
)

FIXTURES = Path(__file__).parent / "fixtures" / "records"
STAMP = datetime(2026, 9, 21, tzinfo=UTC)
EXECUTION = "test-execution"

EV_VALUE = RecordRef(record_id="fixture-ev-values", revision=1)


def load_fixture(name: str) -> list:
    bundle = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return [parse_record(record) for record in bundle["records"]]


def refresh_release_manifest(payload: dict) -> dict:
    """按当前内容重算 release 夹具的清单哈希，用于构造内容被改但格式合法的场景。"""
    from hvs_catalog.records.common import sha256_of

    data = payload["data"]
    data["manifest_sha256"] = sha256_of(
        {
            "data_version": data["data_version"],
            "content_refs": data["content_refs"],
            "artifacts": data["artifacts"],
            "source_run_refs": data["source_run_refs"],
            "code_version": data["code_version"],
            "change_summary": data["change_summary"],
            "channel_targets": [channel["target"] for channel in data["channels"]],
        }
    )
    return payload


def fixture_by_name(records: list, name: str):
    return next(record for record in records if record.RECORD_NAME == name)


def point_value(value_id: str = "val-1", **overrides) -> MeasuredValue:
    fields: dict = {
        "value_id": value_id,
        "value": "520",
        "error": "10",
        "lower_error": None,
        "upper_error": None,
        "unit": "km/s",
        "limit_kind": "none",
        "range_lower": None,
        "range_upper": None,
        "lower_inclusive": None,
        "upper_inclusive": None,
        "interval_kind": None,
        "coverage": None,
        "lower_quantile": None,
        "upper_quantile": None,
        "error_components": [],
        "condition": "New spectrum; heliocentric frame.",
        "frame": "heliocentric",
        "epoch": None,
        "coordinate_format": None,
        "representation": None,
        "purpose": "measurement",
        "source": "this_paper",
        "source_note": None,
        "author_usage": None,
        "direct_evidence": [],
        "context_evidence_refs": [EV_VALUE.model_dump(mode="json")],
    }
    fields.update(overrides)
    if "direct_evidence" not in overrides:
        # 证据组件与实际填写的数值组件保持一致
        ev = EV_VALUE.model_dump(mode="json")
        fields["direct_evidence"] = [
            {"part": name, "evidence_ref": ev, "raw_fragment": fields[name]}
            for name in (
                "value",
                "error",
                "lower_error",
                "upper_error",
                "range_lower",
                "range_upper",
            )
            if fields.get(name) is not None
        ]
    return MeasuredValue.model_validate(fields)


def coverage_rows(
    state: str = "complete",
    checked: list[str] | None = None,
    quantity_overrides: dict[str, dict] | None = None,
) -> list[QuantityCoverageEntry]:
    checked = ["main"] if checked is None else checked
    rows = []
    for quantity in quantity_paths():
        row = {
            "quantity": quantity,
            "state": state,
            "checked_file_ids": list(checked),
            "gap_refs": [],
            "issue_refs": [],
        }
        if quantity_overrides and quantity in quantity_overrides:
            row.update(quantity_overrides[quantity])
        rows.append(QuantityCoverageEntry.model_validate(row))
    return rows


def contribution(
    item_id: str = "contribution-A",
    roster_item_id: str = "roster-A",
    boundness_status: str | None = "bound",
    quantities: list | None = None,
    conflicts: list | None = None,
    coverage: list | None = None,
    evidence_ref: RecordRef | None = None,
    identifiers: list | None = None,
) -> ObjectContribution:
    evidence_ref = evidence_ref or EV_VALUE
    return ObjectContribution.model_validate(
        {
            "item_id": item_id,
            "roster_item_id": roster_item_id,
            "identifiers": identifiers
            or [
                {
                    "value": "FIC-A",
                    "evidence_refs": [evidence_ref.model_dump(mode="json")],
                }
            ],
            "contribution_summary": {
                "text": "Follow-up spectroscopy measures a new heliocentric radial velocity.",
                "evidence_refs": [evidence_ref.model_dump(mode="json")],
            },
            "contribution_type": {
                "value": "follow_up",
                "evidence_refs": [evidence_ref.model_dump(mode="json")],
            },
            "paper_boundness": PaperBoundness.model_validate(
                {
                    "status": boundness_status,
                    "evidence_refs": [evidence_ref.model_dump(mode="json")]
                    if boundness_status
                    else [],
                    "issue_refs": [],
                    "gap_refs": [],
                }
            ),
            "conditional_assessments": [],
            "quantities": [
                {
                    "quantity": "observed_phase_space.radial_velocity",
                    "values": [point_value().model_dump(mode="json")],
                }
            ]
            if quantities is None
            else [
                q if isinstance(q, dict) else q.model_dump(mode="json")
                for q in quantities
            ],
            "quantity_conflicts": [
                c.model_dump(mode="json") for c in (conflicts or [])
            ],
            "quantity_coverage": [
                row.model_dump(mode="json") for row in (coverage or coverage_rows())
            ],
            "precedent_uses": [],
        }
    )


def conflict_pair(issue_ref: RecordRef) -> QuantityConflict:
    return QuantityConflict.model_validate(
        {
            "conflict_id": "conflict-rv",
            "quantity": "observed_phase_space.radial_velocity",
            "reason": "Text and table disagree; no correction is given.",
            "alternatives": [
                point_value("rv-text").model_dump(mode="json"),
                point_value("rv-table", value="525", error="10").model_dump(
                    mode="json"
                ),
            ],
            "issue_ref": issue_ref.model_dump(mode="json"),
        }
    )


def synthetic_source(
    record_id: str = "test-source", paper_key: str = "SYNTHETIC-TEST", gaps=()
) -> SourcePackageRecord:
    return build_record(
        SourcePackageRecord,
        record_id=record_id,
        created_at=STAMP,
        execution_id=EXECUTION,
        data={
            "paper_key": paper_key,
            "origin": {"kind": "synthetic"},
            "source_version": "fictional-v1",
            "files": [
                {
                    "file_id": "main",
                    "path": "materials/complete/paper.tex",
                    "sha256": _hash(FIXTURES / "materials/complete/paper.tex"),
                    "media_type": "text/x-tex",
                    "role": "main_text",
                    "read_state": "ready",
                    "derived_from": [],
                }
            ],
            "gaps": list(gaps),
        },
    )


def _hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()
