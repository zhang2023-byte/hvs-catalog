"""Warnings retain original evidence through selection, publication, and offline reads."""

import copy
import json
from pathlib import Path

import pytest
from test_quantities import quantities, run_full, value
from test_roster import entry, submission
from test_roster_validation import LOCATIONS, message

from hvs_catalog.evaluation.adapt import prediction_from_execution
from hvs_catalog.pipeline.evidence_audit import verify_result_audit
from hvs_catalog.pipeline.results import current_result, saved_quantities
from hvs_catalog.pipeline.roster import saved_roster
from hvs_catalog.records.evidence_audit import QUOTE_WARNING_POLICY, quote_mismatches
from hvs_catalog.records.roster_validation import check_message
from hvs_catalog.records.validate import CollectionValidationError, validate_collection
from hvs_catalog.workspace import (
    WorkspaceIntegrityError,
    backup_workspace,
    restore_workspace,
)
from hvs_catalog.workspace.archives import read_json
from hvs_catalog.workspace.store import record_closure


@pytest.mark.parametrize(
    "quote,reason",
    [
        ("A and B", "whitespace_difference"),
        ("Other block", "exact_match_elsewhere"),
        ("Changed 530", "unknown"),
        ("AandB", "unknown"),
        ("a and B", "unknown"),
        (" ", "unknown"),
    ],
)
def test_quote_classification_is_literal_and_does_not_rewrite(quote, reason):
    locations = {
        "T:p1-b0": {"text": "A\nand\tB; Changed 520"},
        "T:p1-b1": {"text": "Other block"},
        "I:p1-b1": {"text": quote},
    }
    payload = {"evidence": [{"location": "T:p1-b0", "quote": quote}]}
    # The single-space case must actually fail the exact comparison.
    if quote == " ":
        locations["T:p1-b0"]["text"] = "A\nand\tB"
        locations["T:p1-b1"]["text"] = "OtherBlock"
    before = copy.deepcopy((payload, locations))
    mismatch = quote_mismatches(payload, locations)[0]
    assert mismatch.result_path == "/evidence/0"
    assert mismatch.quote == quote and mismatch.reason == reason
    assert (payload, locations) == before


def test_warning_mode_changes_only_nonempty_known_text_matches():
    payload = submission()
    payload["stars"][0]["identifiers"][0]["evidence"] = [
        {"location": "T:p1-b0", "quote": "Unverified text"}
    ]
    prior = message(json.dumps(payload))
    assert not check_message(prior, LOCATIONS).passed
    check = check_message(prior, LOCATIONS, quote_warnings=True)
    assert check.passed and len(check.warnings) == 1
    for evidence in [
        {"location": "T:p9-b9", "quote": "A"},
        {"location": "T:p1-b0", "quote": ""},
        {"location": "T:p1-b0", "quote": None},
        {"location": "T:p1-b0"},
        {"location": "I:p9-b9"},
    ]:
        payload["stars"][0]["identifiers"][0]["evidence"] = [evidence]
        assert not check_message(
            message(json.dumps(payload)), LOCATIONS, quote_warnings=True
        ).passed


def test_warnings_do_not_trigger_correction_and_survive_backup(prepared, tmp_path):
    roster = submission()
    roster["stars"][0]["identifiers"][0]["evidence"] = [
        {"location": "T:p1-b0", "quote": "A  and Alias-A"}
    ]
    numerical = quantities([value("520", "Unverified numerical quote")])
    space = prepared[0]
    handle, report, adapter, _ = run_full(prepared, [roster, numerical])
    assert report.result_status == "complete", report.errors
    assert len(adapter.seen) == 2
    run_ref, summary = current_result(space, handle.execution_id)
    assert not summary.errors
    assert (
        Path(run_ref.path).parent
        == Path(summary.result_ref.path).parent
        == Path(summary.evidence_audit_ref.path).parent
    )
    science = json.loads(space.read_file(summary.result_ref))
    audit = json.loads(space.read_file(summary.evidence_audit_ref))
    assert audit["metadata"] == {
        "arxiv_id": science["metadata"]["arxiv_id"],
        "arxiv_version": "v1",
        "result_ref": summary.result_ref.model_dump(),
    }
    assert audit["quote_mismatches"] == [
        {
            "result_path": "/stars/0/identifiers/0/evidence/0",
            "location": "T:p1-b0",
            "quote": "A  and Alias-A",
            "reason": "whitespace_difference",
        },
        {
            "result_path": "/stars/0/quantities/0/values/0/evidence/0",
            "location": "T:p1-b0",
            "quote": "Unverified numerical quote",
            "reason": "unknown",
        },
    ]
    assert science["stars"][0]["quantities"] == numerical["quantities"]
    trace = read_json(space, summary.trace_ref)
    assert [len(v["warnings"]) for v in trace["validations"]] == [1, 1]
    assert all(v["passed"] and not v["issues"] for v in trace["validations"])
    pred = prediction_from_execution(space, handle.execution_id)
    assert pred.format_valid and pred.completion == "complete"
    assert pred.evidence_checks == {"invalid": 0, "checked": 3, "quote_mismatches": 2}
    backup_workspace(space, tmp_path / "backup")
    restored = restore_workspace(tmp_path / "backup", tmp_path / "restored")
    try:
        assert prediction_from_execution(restored, handle.execution_id) == pred
        assert restored.read_file(summary.evidence_audit_ref) == space.read_file(
            summary.evidence_audit_ref
        )
    finally:
        restored.close()


def test_partial_selection_maps_warnings_to_retained_paths(prepared):
    bad_star = entry("A")
    bad_star["identifiers"][0]["evidence"][0]["location"] = "T:p9-b9"
    good_star = entry("B", 2, "follow_up")
    good_star["identifiers"][0]["evidence"] = [
        {"location": "T:p1-b1", "quote": "Unverified identifier quote"}
    ]
    payload = quantities([value(42, "Rejected quote"), value("700", "Retained quote")])
    handle, report, adapter, _ = run_full(
        prepared, [submission([bad_star, good_star]), payload], limit=0
    )
    assert report.result_status == "partial" and len(adapter.seen) == 2
    space = prepared[0]
    _, summary = current_result(space, handle.execution_id)
    audit = json.loads(space.read_file(summary.evidence_audit_ref))
    assert [w["result_path"] for w in audit["quote_mismatches"]] == [
        "/stars/0/identifiers/0/evidence/0",
        "/stars/0/quantities/0/values/0/evidence/0",
    ]
    assert "Rejected quote" not in json.dumps(audit)
    pred = prediction_from_execution(space, handle.execution_id)
    assert pred.content.stars[0].identifiers[0].value == "B"
    assert pred.content.stars[0].quantities[0].values[0].value == "700"
    assert pred.evidence_checks["quote_mismatches"] == 2


def test_fallback_audits_selected_round_not_last_unreadable_reply(prepared):
    first = quantities([value("520", "Selected quote")])
    first["extra"] = True
    handle, report, adapter, _ = run_full(
        prepared, [submission(), first, "broken"], limit=1
    )
    assert report.result_status == "partial" and len(adapter.seen) == 3
    space = prepared[0]
    _, summary = current_result(space, handle.execution_id)
    assert summary.stages.quantities[0].fallback
    audit = json.loads(space.read_file(summary.evidence_audit_ref))
    assert [w["quote"] for w in audit["quote_mismatches"]] == ["Selected quote"]


@pytest.mark.parametrize("valid", [True, False])
def test_empty_audit_differs_from_no_scientific_result(prepared, valid):
    handle, _, _, _ = run_full(
        prepared, [{"stars": [], "notes": "" if valid else None}], limit=0
    )
    _, summary = current_result(prepared[0], handle.execution_id)
    if valid:
        assert (
            json.loads(prepared[0].read_file(summary.evidence_audit_ref))[
                "quote_mismatches"
            ]
            == []
        )
    else:
        assert summary.result_ref is None and summary.evidence_audit_ref is None


def test_legacy_policy_and_record_serialization_remain_unchanged(prepared, monkeypatch):
    from hvs_catalog.pipeline import inputs

    original = inputs.build_input_snapshot

    def legacy_snapshot(**kwargs):
        kwargs["options"].pop("evidence_policy")
        return original(**kwargs)

    monkeypatch.setattr(inputs, "build_input_snapshot", legacy_snapshot)
    handle, report, _, _ = run_full(
        prepared,
        [submission(), quantities([value(), value("999", "Legacy rejected quote")])],
        limit=0,
    )
    assert report.result_status == "partial"
    space = prepared[0]
    roster = saved_roster(space, handle.execution_id)
    numerical = saved_quantities(space, handle.execution_id, roster)[0]
    assert "evidence_policy" not in numerical.model_dump()["data"]
    assert numerical.data.source_paths == ["/quantities/0/values/0"]
    _, summary = current_result(space, handle.execution_id)
    assert "evidence_audit_ref" not in summary.model_dump()
    pred = prediction_from_execution(space, handle.execution_id)
    assert pred.evidence_checks == {"invalid": 0, "checked": 3}


@pytest.mark.parametrize(
    "tamper",
    [
        "missing",
        "paper",
        "result",
        "quote",
        "location",
        "reason",
        "path",
        "omitted",
        "duplicate",
    ],
)
def test_semantic_audit_tampering_fails_even_with_a_new_file_hash(prepared, tamper):
    space = prepared[0]
    handle, _, _, _ = run_full(
        prepared, [submission(), quantities([value("520", "Unverified")])]
    )
    _, summary = current_result(space, handle.execution_id)
    roster = saved_roster(space, handle.execution_id)
    numerical = saved_quantities(space, handle.execution_id, roster)
    document = read_json(space, summary.input_ref)
    content = prediction_from_execution(space, handle.execution_id).content
    audit = json.loads(space.read_file(summary.evidence_audit_ref))
    if tamper == "missing":
        audit_ref = None
    else:
        if tamper == "paper":
            audit["metadata"]["arxiv_id"] = "9999.99999"
        elif tamper == "result":
            audit["metadata"]["result_ref"]["sha256"] = "0" * 64
        elif tamper == "omitted":
            audit["quote_mismatches"] = []
        elif tamper == "duplicate":
            audit["quote_mismatches"] *= 2
        else:
            field = "result_path" if tamper == "path" else tamper
            audit["quote_mismatches"][0][field] = {
                "quote": "Changed",
                "location": "T:p1-b1",
                "reason": "whitespace_difference",
                "result_path": "/stars/1",
            }[field]
        audit_ref = space.publish_file(
            "runs/forged/evidence_audit.json", json.dumps(audit).encode()
        )
    forged = summary.model_copy(update={"evidence_audit_ref": audit_ref})
    with pytest.raises(ValueError, match="evidence audit"):
        verify_result_audit(space, forged, document, content, roster, numerical)


def test_snapshot_policy_cannot_be_relabelled(prepared):
    space = prepared[0]
    handle, _, _, _ = run_full(
        prepared, [submission(), quantities([value("520", "Unverified")])]
    )
    roster = saved_roster(space, handle.execution_id)
    numerical = saved_quantities(space, handle.execution_id, roster)[0]
    assert numerical.data.evidence_policy == QUOTE_WARNING_POLICY
    changed = numerical.model_copy(
        update={"data": numerical.data.model_copy(update={"evidence_policy": None})}
    )
    closure = record_closure([numerical], space.store().read_record)
    closure = [changed if r.ref() == numerical.ref() else r for r in closure]
    with pytest.raises(CollectionValidationError, match="evidence policy"):
        validate_collection(closure, space.root)


def test_missing_audit_blocks_publication_read_and_scoring(prepared):
    space = prepared[0]
    handle, _, _, _ = run_full(prepared, [submission(), quantities()])
    _, summary = current_result(space, handle.execution_id)
    (space.root / summary.evidence_audit_ref.path).unlink()
    for read in (current_result, prediction_from_execution):
        with pytest.raises((ValueError, FileNotFoundError, WorkspaceIntegrityError)):
            read(space, handle.execution_id)
