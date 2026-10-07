"""Fixed-PDF numerical extraction, independent persistence and policy boundaries."""

from __future__ import annotations

import copy
import json

import pytest
from materials_helpers import FakeMinerU, make_pdf, prepared_space
from pydantic import SecretStr
from test_roster import ScriptedAdapter

from hvs_catalog.llm import RuntimeConfig
from hvs_catalog.llm.managed import CallService
from hvs_catalog.materials import MaterialsService
from hvs_catalog.pipeline import (
    ExtractionConfig,
    resume_extraction,
    start_pdf_extraction,
)
from hvs_catalog.records.quantity_input import (
    bind_quantity_input,
    expand_object_quantity_input,
)
from hvs_catalog.records.submission import SubmissionError
from hvs_catalog.review import ReviewService

LINES = [
    "This paper selects A and B as Galactic unbound candidates. A RV = -430 +/- 12 km/s.",
    "A distance = 2.4 +0.3 -0.2 kpc. A alternative RV = -430 +/- 15 km/s.",
    "B RV = 510 +/- 0 km/s. The paper gives a 90% credible distance interval [2.0, 3.0] kpc.",
    "Under Model P, A RV < -430 km/s; under Model SP, A RV > 0 km/s.",
    "A RV = 4 9 9 $\\pm 6$ km/s.",
    "A distance = -2.4 kpc; distance is defined as non-negative. The authors identify this as a printing error.",
]


def cite(index=0):
    return {"location": f"T:p1-b{index}", "quote": LINES[index]}


def roster(names=("A", "B")):
    return {
        "coverage": {"state": "complete", "checked_file_ids": ["raw"]},
        "unresolved_groups": [],
        "entries": [
            {
                "identifiers": [{"value": name, "evidence": [cite()]}],
                "disposition": "included",
                "evidence": [cite()],
                "summary": "This paper independently selects " + name + ".",
                "summary_evidence": [cite()],
                "contribution_type": "candidates_found",
                "type_evidence": [cite()],
                "boundness": "unbound",
                "boundness_evidence": [cite()],
                "conditional_assessments": [],
            }
            for name in names
        ],
    }


def value(number="-430", error="12", index=0):
    return {
        "value": number,
        "error": error,
        "unit": "km/s",
        "limit_kind": "none",
        "condition": "Reported heliocentric measurement",
        "source": "this_paper",
        "evidence": [cite(index)],
    }


def quantities(object_id, *, values=None, conflicts=None, questions=None):
    groups = (
        values
        if values is not None
        else [{"quantity": "observed_phase_space.radial_velocity", "values": [value()]}]
    )
    return {
        "measurements": [
            {"quantity": g["quantity"], **v} for g in groups for v in g["values"]
        ],
        "quantity_conflicts": conflicts or [],
        "coverage": {"common": {"state": "complete"}, "exceptions": []},
        **(
            {
                "questions": [
                    {k: v for k, v in q.items() if k != "object_id"} for q in questions
                ]
            }
            if questions
            else {}
        ),
    }


class Adapter(ScriptedAdapter):
    def __init__(self, respond, names=("A", "B")):
        super().__init__([])
        self.respond, self.names = respond, names

    def send(self, payload, runtime, credential):
        tool = payload["tools"][0]["function"]
        if tool["name"] == "submit_roster":
            response = roster(self.names)
        else:
            content = payload["messages"][1]["content"]
            text = content[0]["text"] if isinstance(content, list) else content
            context = json.loads(
                text.split("===== SUPPLIED CONTEXT =====\n", 1)[1].split(
                    "\n\n===== APPROVED PRECEDENTS", 1
                )[0]
            )
            target = context["targets"][0]["object_id"]
            response = self.respond({**tool, "_object_id": target}, payload)
        self.seen.append(copy.deepcopy(payload))
        return ScriptedAdapter([response]).send(payload, runtime, credential)


@pytest.fixture
def prepared_pdf(tmp_path):
    space, imported = prepared_space(tmp_path, make_pdf("\n".join(LINES)))
    prep, result = MaterialsService(space, runner=FakeMinerU()).prepare_materials(
        request_key="prepare", paper=imported.canonical_id
    )
    assert result.outcome == "finished"
    config = ExtractionConfig(
        mode="evaluation",
        max_format_corrections=2,
        expert_adjudication=False,
        runtime=RuntimeConfig(quota_store=tmp_path / "quota.sqlite"),
    )
    yield space, prep.execution_id, config
    space.close()


def start(prepared, adapter, expert=False, key="extract"):
    space, prep, config = prepared
    config = config.model_copy(update={"expert_adjudication": expert})
    handle = start_pdf_extraction(
        space, preparation_execution_id=prep, configuration=config, request_key=key
    )
    service = CallService(
        config.runtime, credential=SecretStr("synthetic"), adapter=adapter
    )
    return (
        handle,
        resume_extraction(space, handle.execution_id, service=service),
        service,
    )


def object_id(tool):
    return tool["_object_id"]


def paper(space, execution):
    return space.store().read_record_current(f"pdf-{execution}-paper")


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_large_quantity_reply_preserves_draft_and_correction_budget(
    prepared_pdf, repair_succeeds
):
    raw = quantities("A")
    condition = "Retained measurement condition. " * 6000 + "End of retained condition."
    raw["measurements"][0]["condition"] = condition
    bad = {"location": "T:p1-b0", "quote": "An incorrect quotation"}
    raw["measurements"][0]["evidence"] = [bad]
    original = copy.deepcopy(raw)
    assert len(json.dumps(raw).encode()) > 128 * 1024

    def respond(tool, payload):
        if tool["name"] == "submit_quantities":
            return raw
        assert tool["name"] == "repair_quantities"
        previous = json.loads(
            payload["messages"][-1]["content"].split("Previous output:\n", 1)[1]
        )
        function = previous["tool_calls"][0]["function"]
        arguments = json.loads(function["arguments"])
        if function["name"] == "submit_quantities":
            assert arguments == raw
        else:
            assert function["name"] == "repair_quantities"
            assert arguments and all(value == bad for value in arguments.values())
        return {
            key: cite() if repair_succeeds else bad
            for key in tool["parameters"]["properties"]
        }

    adapter = Adapter(respond, names=("A",))
    handle, report, service = start(prepared_pdf, adapter)
    expected = "finished" if repair_succeeds else "failed"
    assert report.outcome == expected, report.errors
    if not repair_succeeds:
        assert "correction exhausted" in str(report.errors)
    assert len(adapter.seen) == (3 if repair_succeeds else 4)
    assert raw == original
    if repair_succeeds:
        result = paper(prepared_pdf[0], handle.execution_id)
        saved = result.data.object_contributions[0].quantities[0].values[0]
        assert (saved.value, saved.error, saved.condition) == ("-430", "12", condition)
    assert (
        resume_extraction(prepared_pdf[0], handle.execution_id, service=service).outcome
        == expected
    )
    assert len(adapter.seen) == (3 if repair_succeeds else 4)


def test_automatic_extraction_preserves_numbers_and_all_eighteen_completion_entries(
    prepared_pdf,
):
    adapter = Adapter(
        lambda tool, _: (
            quantities(
                object_id(tool),
                values=[
                    {
                        "quantity": "observed_phase_space.radial_velocity",
                        "values": [value("510", "0", 2)],
                    }
                ],
            )
            if object_id(tool).endswith("2")
            else quantities(object_id(tool))
        )
    )
    handle, result, service = start(prepared_pdf, adapter, expert=False)
    assert result.outcome == "finished", result.errors
    contributions = paper(
        prepared_pdf[0], handle.execution_id
    ).data.object_contributions
    assert [c.quantities[0].values[0].value for c in contributions] == ["-430", "510"]
    assert [c.quantities[0].values[0].error for c in contributions] == ["12", "0"]
    assert all(len(c.quantity_coverage) == 18 for c in contributions)
    assert all(
        e.state == "complete" for c in contributions for e in c.quantity_coverage
    )
    assert len(adapter.seen) == 3
    resumed = resume_extraction(prepared_pdf[0], handle.execution_id, service=service)
    assert resumed.outcome == "finished" and len(adapter.seen) == 3


def test_numeric_components_are_associated_without_literal_containment():
    payload = quantities("A")
    bound = bind_quantity_input(
        expand_object_quantity_input(payload, "A"),
        {
            "T:p1-b0": {
                "file_id": "raw",
                "locator": {"kind": "pdf", "page": 1, "block_id": "p1-b0"},
            }
        },
    )
    ev = bound["objects"][0]["quantities"][0]["values"][0]["evidence"][0]
    assert ev["parts"] == [
        {"part": "value", "raw_fragment": "-430"},
        {"part": "error", "raw_fragment": "12"},
    ]
    payload["measurements"][0]["error"] = "999"
    bound = bind_quantity_input(
        expand_object_quantity_input(payload, "A"),
        {
            "T:p1-b0": {
                "file_id": "raw",
                "locator": {"kind": "pdf", "page": 1, "block_id": "p1-b0"},
            }
        },
    )
    assert (
        bound["objects"][0]["quantities"][0]["values"][0]["evidence"][0]["parts"][1][
            "raw_fragment"
        ]
        == "999"
    )


def test_single_object_wire_contract_binds_flat_measurements(prepared_pdf):
    adapter = Adapter(lambda tool, _: quantities(object_id(tool)), names=("A",))
    handle, result, _ = start(prepared_pdf, adapter)
    assert result.outcome == "finished", result.errors
    schema = adapter.seen[1]["tools"][0]["function"]["parameters"]
    assert set(schema["properties"]) == {
        "measurements",
        "quantity_conflicts",
        "coverage",
        "author_errors",
        "model_judgments",
    }
    assert "ObjectQuantitySubmission" not in schema["$defs"]
    assert "QuantityGroupSubmission" not in schema["$defs"]
    assert "ObjectQuestionSubmission" not in schema["$defs"]
    assert (
        len(schema["$defs"]["MeasurementSubmission"]["properties"]["quantity"]["enum"])
        == 18
    )
    obj = paper(prepared_pdf[0], handle.execution_id).data.object_contributions[0]
    assert obj.quantities[0].values[0].value == "-430"


def test_bad_question_container_preserves_independent_complete_measurement(
    prepared_pdf,
):
    def respond(tool, _):
        if tool["name"] == "repair_quantities":
            return {"R1": 123}
        payload = quantities(object_id(tool))
        payload["questions"] = 123
        return payload

    adapter = Adapter(respond, names=("A",))
    handle, result, _ = start(prepared_pdf, adapter, expert=False)
    assert result.outcome == "failed"
    assert (
        len(adapter.seen) == 4
    )  # Roster plus initial quantity call and two corrections.
    obj = paper(prepared_pdf[0], handle.execution_id).data.object_contributions[0]
    assert (obj.quantities[0].values[0].value, obj.quantities[0].values[0].error) == (
        "-430",
        "12",
    )
    assert all(e.state == "partial" for e in obj.quantity_coverage)


def test_missing_field_repairs_do_not_rewrite_measurement(prepared_pdf):
    def respond(tool, _):
        if tool["name"] == "repair_quantities":
            return {"R1": "km/s"}
        payload = quantities(object_id(tool))
        payload["measurements"][0]["unit"] = 12
        return payload

    adapter = Adapter(respond, names=("A",))
    handle, result, _ = start(prepared_pdf, adapter)
    assert result.outcome == "finished", result.errors
    measurement = (
        paper(prepared_pdf[0], handle.execution_id)
        .data.object_contributions[0]
        .quantities[0]
        .values[0]
    )
    assert (measurement.value, measurement.error, measurement.unit) == (
        "-430",
        "12",
        "km/s",
    )
    assert len(adapter.seen) == 3


def test_bad_object_does_not_discard_independent_result(prepared_pdf):
    def respond(tool, _):
        if tool["name"] == "repair_quantities":
            return {"R1": [cite()]}
        payload = quantities(object_id(tool))
        if object_id(tool).endswith("1"):
            payload["measurements"][0]["error"] = {"bad": 999}
        return payload

    adapter = Adapter(respond)
    handle, result, _ = start(prepared_pdf, adapter)
    assert result.outcome == "failed"
    saved = paper(prepared_pdf[0], handle.execution_id)
    assert saved.data.object_contributions[0].quantities == []
    assert saved.data.object_contributions[1].quantities[0].values[0].value == "-430"
    assert len(adapter.seen) == 5


def test_automatic_conflict_has_two_complete_sides_without_expert_task(prepared_pdf):
    conflict = {
        "quantity": "observed_phase_space.radial_velocity",
        "reason": "The same reported RV has conflicting uncertainties.",
        "alternatives": [value(), value(error="15", index=1)],
    }
    adapter = Adapter(
        lambda tool, _: quantities(object_id(tool), values=[], conflicts=[conflict]),
        names=("A",),
    )
    handle, result, _ = start(prepared_pdf, adapter)
    assert result.outcome == "finished", result.errors
    obj = paper(prepared_pdf[0], handle.execution_id).data.object_contributions[0]
    assert obj.quantities == []
    assert [v.error for v in obj.quantity_conflicts[0].alternatives] == ["12", "15"]
    assert (
        ReviewService(prepared_pdf[0]).list_questions(execution_id=handle.execution_id)
        == []
    )


def test_automatic_repair_removes_redundant_expert_request_but_keeps_chosen_science(
    prepared_pdf,
):
    def respond(tool, _):
        if tool["name"] == "repair_quantities":
            return {"R1": None}
        payload = quantities(
            object_id(tool),
            questions=[
                {
                    "object_id": object_id(tool),
                    "quantity": "observed_phase_space.radial_velocity",
                    "category": "interpretation",
                    "prompt": "Would an expert confirm the heliocentric frame?",
                    "evidence": [cite()],
                }
            ],
        )
        payload["model_judgments"] = [
            "I adopt the paper's explicit heliocentric frame."
        ]
        return payload

    adapter = Adapter(respond, names=("A",))
    handle, result, _ = start(prepared_pdf, adapter)
    assert result.outcome == "finished", result.errors
    assert len(adapter.seen) == 3
    assert (
        paper(prepared_pdf[0], handle.execution_id)
        .data.object_contributions[0]
        .quantities[0]
        .values[0]
        .value
        == "-430"
    )
    assert (
        ReviewService(prepared_pdf[0]).list_questions(execution_id=handle.execution_id)
        == []
    )


def test_multiple_conditions_and_repeated_printings(prepared_pdf):
    first = value()
    second = value()
    second["condition"] = "An explicitly separate reference state"
    distance = {
        "value": "2.4",
        "lower_error": "-0.2",
        "upper_error": "+0.3",
        "unit": "kpc",
        "limit_kind": "none",
        "condition": "Reported asymmetric distance",
        "source": "this_paper",
        "evidence": [cite(1)],
    }
    groups = [
        {
            "quantity": "observed_phase_space.radial_velocity",
            "values": [first, copy.deepcopy(first), second],
        },
        {"quantity": "observed_phase_space.distance", "values": [distance]},
    ]
    adapter = Adapter(
        lambda tool, _: quantities(object_id(tool), values=groups), names=("A",)
    )
    handle, result, _ = start(prepared_pdf, adapter)
    assert result.outcome == "finished", result.errors
    saved = paper(prepared_pdf[0], handle.execution_id).data.object_contributions[0]
    assert len(saved.quantities[0].values) == 2
    assert (
        saved.quantities[1].values[0].lower_error,
        saved.quantities[1].values[0].upper_error,
    ) == ("-0.2", "+0.3")


def test_interval_without_invented_central_value(prepared_pdf):
    interval = {
        "range_lower": "2.0",
        "range_upper": "3.0",
        "lower_inclusive": True,
        "upper_inclusive": True,
        "interval_kind": "credible",
        "coverage": {"value": "90", "unit": "%", "evidence": [cite(2)]},
        "unit": "kpc",
        "limit_kind": "range",
        "condition": "Reported credible distance interval",
        "source": "this_paper",
        "evidence": [cite(2)],
    }
    adapter = Adapter(
        lambda tool, _: quantities(
            object_id(tool),
            values=[
                {"quantity": "observed_phase_space.distance", "values": [interval]}
            ],
        ),
        names=("A",),
    )
    handle, result, _ = start(prepared_pdf, adapter)
    assert result.outcome == "finished", result.errors
    saved = (
        paper(prepared_pdf[0], handle.execution_id)
        .data.object_contributions[0]
        .quantities[0]
        .values[0]
    )
    assert saved.value is None and saved.coverage.value == "90"
    assert saved.range_lower == "2.0" and saved.range_upper == "3.0"


def source_error(quantity="observed_phase_space.distance"):
    return {
        "quantity": quantity,
        "condition": "Reported heliocentric distance",
        "source": "this_paper",
        "raw_values": ["-2.4 kpc"],
        "reason": "The paper explicitly defines distance as non-negative and identifies the printed negative distance as an error.",
        "evidence": [cite(5)],
    }


def test_source_error_isolated_and_restored_without_human_tasks(prepared_pdf, tmp_path):
    from hvs_catalog.pipeline import get_extraction
    from hvs_catalog.workspace import backup_workspace, restore_workspace

    def respond(tool, _):
        result = quantities(object_id(tool))
        if object_id(tool).endswith("1"):
            result["author_errors"] = [source_error()]
        return result

    adapter = Adapter(respond)
    handle, result, service = start(prepared_pdf, adapter)
    assert result.outcome == "finished", result.errors
    space = prepared_pdf[0]
    saved = paper(space, handle.execution_id)
    first, second = saved.data.object_contributions
    assert first.quantities[0].values[0].value == "-430"
    assert second.quantities[0].values[0].value == "-430"
    assert first.author_errors[0].raw_values == ["-2.4 kpc"]
    assert {c.quantity for c in first.quantity_coverage if c.state == "partial"} == {
        "observed_phase_space.distance"
    }
    status = get_extraction(space, handle.execution_id)
    assert status["science_completion"]["state"] == "partial"
    assert status["questions"] == [] and len(status["diagnostics"]) == 1
    diagnostic = status["diagnostics"][0]
    assert diagnostic["item_id"] == first.item_id and diagnostic[
        "source_ref"
    ] == saved.data.source_ref.model_dump(mode="json")
    assert diagnostic["evidence_refs"]
    other, finished, _ = start(
        prepared_pdf,
        Adapter(lambda tool, _: quantities(object_id(tool)), names=("A",)),
        key="other",
    )
    assert finished.outcome == "finished"
    other_before = paper(space, other.execution_id).ref()
    assert (
        resume_extraction(space, handle.execution_id, service=service).outcome
        == "finished"
    )
    assert (
        len(adapter.seen) == 3
        and paper(space, other.execution_id).ref() == other_before
    )
    backup_workspace(space, tmp_path / "backup")
    restored = restore_workspace(tmp_path / "backup", tmp_path / "restored")
    try:
        assert (
            get_extraction(restored, handle.execution_id)["diagnostics"]
            == status["diagnostics"]
        )
        assert (
            resume_extraction(restored, handle.execution_id, service=service).outcome
            == "finished"
        )
        assert len(adapter.seen) == 3
    finally:
        restored.close()


def test_failed_measurement_keeps_other_complete_values_in_same_object(prepared_pdf):
    def respond(tool, _):
        if tool["name"] == "repair_quantities":
            return {"R1": [cite()]}
        valid = value()
        invalid = value(error={"bad": 999})
        payload = quantities(
            object_id(tool),
            values=[
                {
                    "quantity": "observed_phase_space.radial_velocity",
                    "values": [valid, invalid],
                }
            ],
        )
        payload["coverage"]["exceptions"] = [
            {"quantity": "observed_phase_space.radial_velocity", "state": "partial"}
        ]
        return payload

    adapter = Adapter(respond, names=("A",))
    handle, result, _ = start(prepared_pdf, adapter)
    assert result.outcome == "failed"
    obj = paper(prepared_pdf[0], handle.execution_id).data.object_contributions[0]
    assert len(obj.quantities[0].values) == 1
    assert obj.quantities[0].values[0].error == "12"
    assert all(c.state == "partial" for c in obj.quantity_coverage)
    questions = ReviewService(prepared_pdf[0]).list_questions(
        execution_id=handle.execution_id
    )
    assert questions == []


def test_surplus_root_brace_is_application_repair_without_scientific_rewrite(
    prepared_pdf,
):
    adapter = Adapter(
        lambda tool, _: json.dumps(quantities(object_id(tool))) + "}", names=("A",)
    )
    handle, result, _ = start(prepared_pdf, adapter)
    assert result.outcome == "finished", result.errors
    assert len(adapter.seen) == 2
    space = prepared_pdf[0]
    repairs = list(
        (space.root / f"runs/{handle.execution_id}/outputs/format-cleanups").glob(
            "*.json"
        )
    )
    assert len(repairs) == 1
    run = space.store().read_record_current(f"run-{handle.execution_id}")
    assert len(run.data.artifacts) == 1
    assert space.read_file(run.data.artifacts[0])
    assert (
        paper(space, handle.execution_id)
        .data.object_contributions[0]
        .quantities[0]
        .values[0]
        .value
        == "-430"
    )


def test_annotated_citation_ids_are_bound_without_extra_model_calls(prepared_pdf):
    def respond(tool, _):
        payload = quantities(object_id(tool))
        payload["measurements"][0]["evidence"][0]["location"] += (
            " (paragraph, reported RV)"
        )
        return payload

    adapter = Adapter(respond, names=("A",))
    handle, result, _ = start(prepared_pdf, adapter, expert=False)
    assert result.outcome == "finished", result.errors
    assert len(adapter.seen) == 2
    space = prepared_pdf[0]
    run = space.store().read_record_current(f"run-{handle.execution_id}")
    cleanup = json.loads(space.read_file(run.data.artifacts[0]))
    assert cleanup["changes"][0]["before"]["location"].endswith(
        "(paragraph, reported RV)"
    )
    assert cleanup["changes"][0]["after"]["location"] == "T:p1-b0"
    assert (
        cleanup["changes"][0]["before"]["quote"]
        == cleanup["changes"][0]["after"]["quote"]
    )
    assert (
        paper(space, handle.execution_id)
        .data.object_contributions[0]
        .quantities[0]
        .values[0]
        .value
        == "-430"
    )


def test_example_id_cannot_be_used_as_unsupplied_precedent(prepared_pdf):
    def respond(tool, _):
        if tool["name"] == "repair_quantities":
            assert set(tool["parameters"]["properties"]) == {"R1"}
            assert tool["parameters"]["properties"]["R1"]["type"] == "null"
            return {"R1": None}
        payload = quantities(object_id(tool))
        payload["precedent_uses"] = [
            {
                "precedent_id": "E14",
                "explanation": "Preserve the printed values, as the rule example illustrates.",
            }
        ]
        return payload

    adapter = Adapter(respond, names=("A",))
    handle, result, _ = start(prepared_pdf, adapter, expert=False)
    assert result.outcome == "finished", result.errors
    assert len(adapter.seen) == 3
    contribution = paper(
        prepared_pdf[0], handle.execution_id
    ).data.object_contributions[0]
    assert contribution.precedent_uses == []
    assert contribution.quantities[0].values[0].value == "-430"
    assert contribution.quantities[0].values[0].error == "12"


@pytest.mark.parametrize(
    "kind,endpoint,number",
    [("upper_limit", "range_upper", "-430"), ("lower_limit", "range_lower", "0")],
)
def test_one_sided_limit_endpoint_is_saved_without_an_extra_model_call(
    prepared_pdf, kind, endpoint, number
):
    def respond(tool, _):
        limited = {
            "limit_kind": kind,
            endpoint: number,
            "unit": "km/s",
            "condition": "Model P" if kind == "upper_limit" else "Model SP",
            "source": "this_paper",
            "evidence": [cite(3)],
        }
        return quantities(
            object_id(tool),
            values=[
                {
                    "quantity": "observed_phase_space.radial_velocity",
                    "values": [limited],
                }
            ],
        )

    adapter = Adapter(respond, names=("A",))
    handle, result, _ = start(prepared_pdf, adapter)
    assert result.outcome == "finished", result.errors
    assert len(adapter.seen) == 2
    saved = (
        paper(prepared_pdf[0], handle.execution_id)
        .data.object_contributions[0]
        .quantities[0]
        .values[0]
    )
    assert saved.value == number and saved.limit_kind == kind
    assert saved.range_lower is None and saved.range_upper is None
    assert saved.direct_evidence[0].part == "value"
    run = prepared_pdf[0].store().read_record_current(f"run-{handle.execution_id}")
    cleanup = json.loads(prepared_pdf[0].read_file(run.data.artifacts[0]))
    assert cleanup["changes"][0]["raw_value"] == number


@pytest.mark.parametrize(
    "raw",
    [
        {"limit_kind": "upper_limit", "value": "0.4", "range_upper": "0.5"},
        {"limit_kind": "upper_limit", "range_lower": "0.3", "range_upper": "0.5"},
        {"limit_kind": "range", "range_lower": "0.3", "range_upper": "0.5"},
    ],
)
def test_limit_operand_canonicalization_does_not_choose_between_numbers(raw):
    from hvs_catalog.records.quantity_input import canonicalize_limit_operands

    result, changes = canonicalize_limit_operands(raw)
    assert result == raw and changes == []


@pytest.mark.parametrize("matching", [True, False])
def test_redundant_quote_escaping_requires_exact_fixed_text_match(
    prepared_pdf, monkeypatch, matching
):
    from types import SimpleNamespace

    from hvs_catalog.pipeline import roster as module

    saved = []
    monkeypatch.setattr(module, "append_run_revision", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "hvs_catalog.records.pdf.pdf_evidence_block",
        lambda *args, **kwargs: {"text": r"RV = -430 \pm 12"},
    )

    class Space:
        root = prepared_pdf[0].root

        def publish_run_file(self, *args):
            saved.append(json.loads(args[-1]))

    ctx = SimpleNamespace(
        space=Space(), execution=SimpleNamespace(execution_id="synthetic")
    )
    inputs = SimpleNamespace(
        citation_locations={
            "T:p1-b0": {
                "file_id": "raw",
                "locator": {"kind": "pdf", "page": 1, "block_id": "p1-b0"},
            }
        }
    )
    payload = {
        "evidence": [
            {
                "location": "T:p1-b0",
                "quote": r"-430 \\pm 12" if matching else r"-431 \\pm 12",
            }
        ]
    }
    result = module._clean_citations(
        ctx, payload, inputs, None, "quantities", "synthetic"
    )
    assert payload["evidence"][0]["quote"].count("\\") == 2
    if matching:
        assert result["evidence"][0]["quote"] == r"-430 \pm 12"
        assert len(saved) == 1
    else:
        assert result == payload
        assert saved == []


@pytest.mark.parametrize(
    "arguments",
    [
        '{"value":"499","value":"5"}}',
        '{"value":NaN}}',
        '{"value":"499"}{"value":"5"}',
        '{"value":"499"} trailing',
        '{"value":"499"',
        '{"value":"499"}}}',
    ],
)
def test_application_json_repair_rejects_ambiguous_or_incomplete_science(arguments):
    from hvs_catalog.pipeline.roster import _prior_submission

    assert (
        _prior_submission({"tool_calls": [{"function": {"arguments": arguments}}]})
        is None
    )


def test_parser_spacing_keeps_number_and_valid_text_evidence(prepared_pdf):
    adapter = Adapter(
        lambda tool, _: quantities(
            object_id(tool),
            values=[
                {
                    "quantity": "observed_phase_space.radial_velocity",
                    "values": [value("499", "6", 4)],
                }
            ],
        ),
        names=("A",),
    )
    handle, result, _ = start(prepared_pdf, adapter)
    assert result.outcome == "finished", result.errors
    space = prepared_pdf[0]
    saved = (
        paper(space, handle.execution_id)
        .data.object_contributions[0]
        .quantities[0]
        .values[0]
    )
    evidence = space.store().read_record(saved.direct_evidence[0].evidence_ref)
    assert evidence.data.locator.kind == "pdf"
    assert evidence.data.excerpt == LINES[4]
    assert saved.value == "499" and saved.error == "6"
    assert not any(
        p["type"] == "image_url"
        for m in adapter.seen[1]["messages"]
        if isinstance(m["content"], list)
        for p in m["content"]
    )


def test_recovery_rejects_object_revision_without_bound_decision(prepared_pdf):
    from types import SimpleNamespace

    from hvs_catalog.pipeline.quantities import PDFExtractionFlow
    from hvs_catalog.workspace import CommitRequest

    adapter = Adapter(
        lambda tool, _: {
            **quantities(object_id(tool)),
            "author_errors": [source_error()],
        },
        names=("A",),
    )
    handle, result, service = start(prepared_pdf, adapter)
    assert result.outcome == "finished"
    space = prepared_pdf[0]
    run = space.store().read_record_current(f"run-{handle.execution_id}")
    step = next(
        s
        for s in run.data.steps
        if s.step_id.startswith("quantities-") and s.result_refs
    )
    unit = space.store().read_record(step.result_refs[0])
    space.store().commit(
        CommitRequest(
            operation_key="unrelated-unit-revision",
            kind="synthetic-interference",
            new_records=[unit.model_copy(update={"revision": unit.revision + 1})],
            expected_revisions={unit.record_id: unit.revision},
            execution_id=handle.execution_id,
            step_id=step.step_id,
        )
    )
    recovered = resume_extraction(space, handle.execution_id, service=service)
    # Waiting for an answer still pauses, and aggregation refuses the unrelated
    # revision rather than selecting the newest object contents.
    assert recovered.outcome == "finished"
    ctx = SimpleNamespace(space=space, store=space.store(), execution=handle)
    with pytest.raises(SubmissionError, match="no matching applied decision receipt"):
        PDFExtractionFlow(mode="evaluation")._applied_record(ctx, unit.ref())
    assert len(adapter.seen) == 2


def test_all_quantities_are_explicitly_unreported_after_empty_checked_submission(
    prepared_pdf,
):
    adapter = Adapter(
        lambda tool, _: quantities(object_id(tool), values=[]), names=("A",)
    )
    handle, result, _ = start(prepared_pdf, adapter)
    assert result.outcome == "finished", result.errors
    obj = paper(prepared_pdf[0], handle.execution_id).data.object_contributions[0]
    assert not obj.quantities and len(obj.quantity_coverage) == 18
    assert all(
        c.state == "complete" and c.checked_file_ids == ["raw"]
        for c in obj.quantity_coverage
    )


def test_program_binds_known_material_gap_only_to_declared_affected_quantity(tmp_path):
    from hvs_catalog.records.source import Gap
    from hvs_catalog.workspace import CommitRequest

    space, imported = prepared_space(tmp_path, make_pdf(LINES[0]))
    try:
        source = space.store().read_record(imported.material_ref)
        gap = Gap.model_validate(
            {
                "gap_id": "missing-distance-appendix",
                "reason": "external_not_included",
                "description": "A separately mentioned fictional distance appendix is absent.",
                "file_id": None,
                "mention_evidence_refs": [],
                "affected_targets": {
                    "scope": "known",
                    "items": [
                        {
                            "item_id": None,
                            "quantity": "observed_phase_space.distance",
                            "judgment": None,
                        }
                    ],
                },
            }
        )
        source = source.model_copy(
            update={
                "revision": source.revision + 1,
                "data": source.data.model_copy(update={"gaps": [gap]}),
            }
        )
        space.store().commit(
            CommitRequest(
                operation_key="fixed-synthetic-gap",
                kind="synthetic-source",
                new_records=[source],
                expected_revisions={source.record_id: source.revision - 1},
            )
        )
        prep, prepared = MaterialsService(space, runner=FakeMinerU()).prepare_materials(
            request_key="prepare", source_ref=source.ref()
        )
        assert prepared.outcome == "finished"
        config = ExtractionConfig(
            mode="evaluation",
            expert_adjudication=False,
            runtime=RuntimeConfig(quota_store=tmp_path / "quota.sqlite"),
        )

        def respond(tool, _):
            payload = quantities(object_id(tool))
            payload["coverage"]["exceptions"] = [
                {"quantity": "observed_phase_space.distance", "state": "partial"}
            ]
            return payload

        handle, result, _ = start(
            (space, prep.execution_id, config), Adapter(respond, names=("A",))
        )
        assert result.outcome == "finished", result.errors
        rows = (
            paper(space, handle.execution_id)
            .data.object_contributions[0]
            .quantity_coverage
        )
        distance = next(
            c for c in rows if c.quantity == "observed_phase_space.distance"
        )
        assert distance.state == "partial" and [
            g.gap_id for g in distance.gap_refs
        ] == [gap.gap_id]
        assert all(
            c.state == "complete" and not c.gap_refs
            for c in rows
            if c.quantity != distance.quantity
        )
    finally:
        space.close()


@pytest.mark.parametrize("valid_diagnostic", [True, False])
def test_failed_submission_never_recovers_diagnosed_measurement(
    prepared_pdf, valid_diagnostic
):
    def respond(tool, _):
        diagnostic = source_error()
        if not valid_diagnostic:
            diagnostic["evidence"] = []
        distance = value("-2.4", None, 5)
        distance.update(unit="kpc", condition=diagnostic["condition"])
        result = quantities(
            object_id(tool),
            values=[
                {
                    "quantity": "observed_phase_space.radial_velocity",
                    "values": [value()],
                },
                {"quantity": diagnostic["quantity"], "values": [distance]},
            ],
        )
        result["author_errors"] = [diagnostic]
        return result

    handle, result, _ = start(prepared_pdf, Adapter(respond, names=("A",)))
    assert result.outcome == "failed"
    obj = paper(prepared_pdf[0], handle.execution_id).data.object_contributions[0]
    assert [g.quantity for g in obj.quantities] == [
        "observed_phase_space.radial_velocity"
    ]
    assert len(obj.author_errors) == int(valid_diagnostic)
    assert all(c.state != "complete" for c in obj.quantity_coverage)
