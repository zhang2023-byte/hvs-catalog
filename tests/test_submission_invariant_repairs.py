"""Approved structural invariants are repaired without editing numeric evidence."""

import copy

import pytest
import test_pdf_quantities as fixtures
from pydantic import SecretStr
from test_roster import ScriptedAdapter

from hvs_catalog.llm.managed import CallService
from hvs_catalog.pipeline import (
    get_extraction,
    resume_extraction,
    start_roster_extraction,
)
from hvs_catalog.pipeline.quantities import quantity_repairs
from hvs_catalog.pipeline.roster_repair import apply_repairs, roster_invariant_repairs


@pytest.fixture
def prepared(tmp_path):
    yield from fixtures.prepared_pdf.__wrapped__(tmp_path)


def fixed_replacements(tool):
    return {
        name: copy.deepcopy(field.get("const"))
        for name, field in tool["parameters"]["properties"].items()
    }


def test_declared_roster_source_error_is_isolated_through_bounded_repair(prepared):
    space, prep, config = prepared
    raw = fixtures.roster(("A",))
    diagnostic = {
        "reason": "Original identity is explicitly inconsistent.",
        "raw_values": ["A"],
        "evidence": [fixtures.cite()],
    }
    raw["entries"][0]["author_errors"] = [diagnostic]
    before = copy.deepcopy(raw)
    fixes = roster_invariant_repairs(raw)
    corrected = {
        f"R{i}": copy.deepcopy(fix.schema.get("const"))
        for i, fix in enumerate(fixes, 1)
    }
    adapter = ScriptedAdapter([raw, corrected])
    handle = start_roster_extraction(
        space,
        preparation_execution_id=prep,
        configuration=config,
        request_key="source-error",
    )
    report = resume_extraction(
        space,
        handle.execution_id,
        service=CallService(
            config.runtime, credential=SecretStr("synthetic"), adapter=adapter
        ),
    )
    assert report.outcome == "finished", report.errors
    assert raw == before and len(adapter.seen) == 2
    assert adapter.seen[1]["tools"][0]["function"]["name"] == "repair_roster"
    status = get_extraction(space, handle.execution_id)
    assert status["science_completion"]["state"] == "partial"
    assert not status["questions"]
    patched = apply_repairs(raw, fixes, corrected)
    assert patched["entries"][0]["author_errors"] == [diagnostic]
    assert patched["entries"][0]["identifiers"] == raw["entries"][0]["identifiers"]
    assert patched["entries"][0]["disposition"] == "pending"


def test_different_scope_conflict_moves_unchanged_alternatives_after_model_confirmation(
    prepared,
):
    first, second = fixtures.value(), fixtures.value(error="15", index=1)
    first["condition"], second["condition"] = "under Model P", "under Model SP"
    conflict = {
        "quantity": "observed_phase_space.radial_velocity",
        "reason": "Different declared models.",
        "alternatives": [first, second],
    }

    def respond(tool, _):
        if tool["name"] == "repair_quantities":
            return fixed_replacements(tool)
        return fixtures.quantities(
            fixtures.object_id(tool), values=[], conflicts=[copy.deepcopy(conflict)]
        )

    adapter = fixtures.Adapter(respond, names=("A",))
    handle, result, _ = fixtures.start(prepared, adapter)
    assert result.outcome == "finished", result.errors
    obj = fixtures.paper(prepared[0], handle.execution_id).data.object_contributions[0]
    assert not obj.quantity_conflicts
    assert [(v.value, v.error, v.condition) for v in obj.quantities[0].values] == [
        ("-430", "12", "under Model P"),
        ("-430", "15", "under Model SP"),
    ]
    assert len(adapter.seen) == 3
    assert first["error"] == "12" and second["error"] == "15"


def test_inapplicable_interval_label_repair_preserves_point_and_error(prepared):
    def respond(tool, _):
        if tool["name"] == "repair_quantities":
            return fixed_replacements(tool)
        raw = fixtures.quantities(fixtures.object_id(tool))
        raw["measurements"][0]["interval_kind"] = "credible"
        return raw

    adapter = fixtures.Adapter(respond, names=("A",))
    handle, result, _ = fixtures.start(prepared, adapter)
    assert result.outcome == "finished", result.errors
    value = (
        fixtures.paper(prepared[0], handle.execution_id)
        .data.object_contributions[0]
        .quantities[0]
        .values[0]
    )
    assert (value.value, value.error, value.interval_kind) == ("-430", "12", None)
    assert len(adapter.seen) == 3


def test_repairs_do_not_reinterpret_same_scope_or_choose_numeric_components():
    value = fixtures.value()
    raw = {
        "measurements": [value],
        "quantity_conflicts": [
            {
                "quantity": "observed_phase_space.radial_velocity",
                "alternatives": [value, fixtures.value(error="15")],
            }
        ],
    }
    assert quantity_repairs(raw, {}, "不同条件下的结果应分别保存") == []
    assert (
        quantity_repairs(
            raw, {}, "纯区间不能补中心值，端点写入 range_lower/range_upper"
        )
        == []
    )
    assert quantity_repairs(raw, {}, "unrecognized scientific interpretation") == []


def test_failed_roster_resume_preserves_all_observable_facts(prepared):
    space, prep, config = prepared
    adapter = ScriptedAdapter(["truncated"])
    service = CallService(
        config.runtime, credential=SecretStr("synthetic"), adapter=adapter
    )
    handle = start_roster_extraction(
        space, preparation_execution_id=prep, configuration=config, request_key="failed"
    )
    assert (
        resume_extraction(space, handle.execution_id, service=service).outcome
        == "failed"
    )
    before = get_extraction(space, handle.execution_id)
    count = len(adapter.seen)
    resumed = resume_extraction(space, handle.execution_id, service=service)
    assert resumed.outcome == "failed" and resumed.mode == "already-failed"
    assert get_extraction(space, handle.execution_id) == before
    assert len(adapter.seen) == count
