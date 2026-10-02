"""Legacy contribution Gold v4 read adapter: structural rejection, canaries, and reproduced
science validation.

Use only synthetic SYN-* materials, without real answers or papers.
"""

from __future__ import annotations

import copy
import json

import pytest
from evaluation_helpers import (
    build_legacy_contribution,
    build_legacy_range_value,
    build_legacy_v4_doc,
)

from hvs_catalog.evaluation import (
    LegacyFormatError,
    legacy_canary,
    parse_legacy_annotation,
    verify_legacy_canary,
)


def test_valid_v4_document_parses_with_fields():
    document = build_legacy_v4_doc()
    annotation = parse_legacy_annotation(document)
    assert annotation.schema_tag.name == "benchmark.hvs_contribution_annotation"
    assert annotation.schema_tag.version == 4
    assert annotation.arxiv_id == "2501.00001"
    assert annotation.status == "contributions_found"
    (contribution,) = annotation.contributions
    assert contribution.identifiers[0].value == "SYN-A"
    (group,) = contribution.quantities
    assert group.quantity == "observed_phase_space.radial_velocity"
    (value,) = group.values
    assert (value.value, value.error, value.unit) == ("612.0", "8.0", "km/s")
    assert value.evidence[0].location == "Table 2, row SYN-A"


@pytest.mark.parametrize("version", [1, 2, 3, 5])
def test_unsupported_version_rejected(version):
    document = build_legacy_v4_doc()
    document["schema"]["version"] = version
    with pytest.raises(LegacyFormatError, match="不在受支持范围"):
        parse_legacy_annotation(document)


def test_unknown_schema_name_rejected():
    document = build_legacy_v4_doc()
    document["schema"]["name"] = "benchmark.other_annotation"
    with pytest.raises(LegacyFormatError, match="schema.name"):
        parse_legacy_annotation(document)


def test_unknown_field_rejected():
    document = build_legacy_v4_doc()
    document["contributions"][0]["quantities"][0]["values"][0]["paper_preferred"] = True
    with pytest.raises(LegacyFormatError, match="结构不满足"):
        parse_legacy_annotation(document)


def test_corrupted_json_rejected():
    with pytest.raises(LegacyFormatError, match="不是有效的 JSON"):
        parse_legacy_annotation("{not json")


def test_non_object_rejected():
    with pytest.raises(LegacyFormatError, match="JSON 对象"):
        parse_legacy_annotation([1, 2])


def test_canary_roundtrip_and_mismatch():
    document = build_legacy_v4_doc()
    assert verify_legacy_canary(parse_legacy_annotation(document), document=document)
    tampered = copy.deepcopy(document)
    tampered["contributions"][0]["contribution_summary"] += " changed"
    assert not verify_legacy_canary(
        parse_legacy_annotation(tampered), document=tampered
    )
    without_canary = copy.deepcopy(document)
    without_canary.pop("canary")
    assert legacy_canary(without_canary) == document["canary"]


def test_missing_canary_is_not_an_error():
    document = build_legacy_v4_doc()
    document["canary"] = ""
    annotation = parse_legacy_annotation(document)
    assert verify_legacy_canary(
        annotation, document=document
    )  # No verification basis exists; do not mark an error.


def test_mixed_uncertainties_rejected():
    document = build_legacy_v4_doc()
    value = document["contributions"][0]["quantities"][0]["values"][0]
    value["lower_error"] = "7.0"
    value["upper_error"] = "9.0"
    with pytest.raises(LegacyFormatError, match="cannot mix"):
        parse_legacy_annotation(document)


def test_not_assessed_allows_missing_evidence_but_unbound_requires_it():
    document = build_legacy_v4_doc(
        contributions=[
            build_legacy_contribution(
                boundness_status="not_assessed", contribution_type="follow_up"
            )
        ]
    )
    parse_legacy_annotation(document)  # not_assessed is acceptable without evidence.
    document["contributions"][0]["paper_boundness"]["status"] = "unbound"
    with pytest.raises(LegacyFormatError, match="evidence is required"):
        parse_legacy_annotation(document)


def test_conflict_needs_two_distinct_locators():
    document = build_legacy_v4_doc()
    document["contributions"][0]["quantity_conflicts"] = [
        {
            "quantity": "observed_phase_space.distance",
            "reason": "two printed distances",
            "evidence": [
                {"location": "Table 3", "quote": ""},
                {"location": "Table 3", "quote": ""},
            ],
        }
    ]
    with pytest.raises(LegacyFormatError, match="two distinct"):
        parse_legacy_annotation(document)


def test_duplicate_value_rejected():
    document = build_legacy_v4_doc()
    value = document["contributions"][0]["quantities"][0]["values"][0]
    document["contributions"][0]["quantities"][0]["values"].append(
        json.loads(json.dumps(value))
    )
    with pytest.raises(LegacyFormatError, match="duplicate reported value"):
        parse_legacy_annotation(document)


def test_range_value_with_coverage_and_quantiles_roundtrip():
    document = build_legacy_v4_doc()
    range_value = build_legacy_range_value()
    range_value["lower_quantile"] = {
        "value": "5",
        "unit": "%",
        "evidence": [{"location": "Table 3 caption", "quote": ""}],
    }
    range_value["upper_quantile"] = {
        "value": "95",
        "unit": "%",
        "evidence": [{"location": "Table 3 caption", "quote": ""}],
    }
    document["contributions"][0]["quantities"].append(
        {
            "quantity": "observed_phase_space.distance",
            "values": [range_value],
        }
    )
    annotation = parse_legacy_annotation(document)
    (group,) = [
        g
        for g in annotation.contributions[0].quantities
        if g.quantity == "observed_phase_space.distance"
    ]
    (value,) = group.values
    assert value.interval_kind == "confidence"
    assert value.coverage.value == "90"
    assert value.lower_quantile.value == "5"


def test_quantile_ordering_enforced():
    document = build_legacy_v4_doc()
    range_value = build_legacy_range_value()
    range_value["lower_quantile"] = {
        "value": "95",
        "unit": "%",
        "evidence": [{"location": "Table 3 caption", "quote": ""}],
    }
    range_value["upper_quantile"] = {
        "value": "5",
        "unit": "%",
        "evidence": [{"location": "Table 3 caption", "quote": ""}],
    }
    document["contributions"][0]["quantities"].append(
        {"quantity": "observed_phase_space.distance", "values": [range_value]}
    )
    with pytest.raises(LegacyFormatError, match="below upper quantile"):
        parse_legacy_annotation(document)


def test_coordinate_range_enforced():
    document = build_legacy_v4_doc()
    document["contributions"][0]["quantities"].append(
        {
            "quantity": "observed_phase_space.dec",
            "values": [
                {
                    "value": "-95.0",
                    "error": "0.1",
                    "unit": "deg",
                    "limit_kind": "none",
                    "coordinate_format": "decimal_degrees",
                    "condition": "J2016",
                    "source": "this_paper",
                    "evidence": [{"location": "Table 1", "quote": ""}],
                }
            ],
        }
    )
    with pytest.raises(LegacyFormatError, match="out of range"):
        parse_legacy_annotation(document)


def test_migration_protocol_requires_process_fields():
    document = build_legacy_v4_doc()
    document["annotation_process"]["preannotation_model"] = ""
    with pytest.raises(LegacyFormatError, match="migration protocol"):
        parse_legacy_annotation(document)


def test_v4_rejects_pending_stage_b_placeholder():
    document = build_legacy_v4_doc()
    document["annotation_process"]["reconciliation_agent"] = "PENDING_STAGE_B"
    with pytest.raises(LegacyFormatError, match="PENDING_STAGE_B"):
        parse_legacy_annotation(document)
