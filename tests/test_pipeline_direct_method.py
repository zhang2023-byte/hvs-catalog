"""Shared extraction entry point: mock direct execution and roster-to-value transfer
(C02/C03).

Roster outputs are isolated by execution. Values accept included entries only; excluded
or unresolved response objects fail explicitly. Later failures retain saved rosters, and
identical recovery input reuses them without another mock service call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hvs_catalog.pipeline import (
    PAPER_A,
    PAPER_B,
    ExtractionRequest,
    StepModel,
    get_extraction,
    import_mock_source,
    resume_extraction,
    start_extraction,
)
from hvs_catalog.pipeline.mock import PipelineMockService, material_spec
from hvs_catalog.records.common import RecordRef
from hvs_catalog.records.paper import PaperRecord
from hvs_catalog.records.roster import RosterRecord
from hvs_catalog.workspace import initialize_workspace

FIXTURES = Path(__file__).parent / "fixtures" / "pipeline"
MATERIALS = json.loads((FIXTURES / "materials.json").read_text(encoding="utf-8"))


@pytest.fixture()
def space(tmp_path):
    workspace = initialize_workspace(tmp_path / "space")
    yield workspace
    workspace.close()


@pytest.fixture()
def service():
    return PipelineMockService()


def _start(space, source, *, request_key, mode="daily", expert=True, models=None):
    request = ExtractionRequest(
        mode=mode,
        method="direct",
        source_refs=[source],
        models=models or [StepModel(model="mock-default")],
        expert_adjudication=expert,
    )
    return start_extraction(space, request=request, request_key=request_key)


def _record(space, record_id):
    revision = space.store().current_revision(record_id)
    assert revision is not None
    record = space.store().read_record(
        RecordRef(record_id=record_id, revision=revision)
    )
    assert record is not None
    return record


def test_direct_clear_paper_finishes_without_pause(space, service):
    """Unambiguous paper B completes once with default expert review enabled and no
    questions.
    """
    source = import_mock_source(space, PAPER_B)
    handle = _start(space, source, request_key="b-on")
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished"
    assert report.errors == []
    assert report.pending_question is None
    assert report.open_questions == 0
    roster = _record(space, f"roster-fic-r05-b-{handle.execution_id}")
    assert isinstance(roster, RosterRecord)
    assert roster.data.method == "direct"
    assert [entry.disposition for entry in roster.data.entries] == ["included"]
    expected = MATERIALS["papers"]["FIC-R05-B"]["values"][0]
    paper = _record(space, f"paper-fic-r05-b-{handle.execution_id}")
    assert isinstance(paper, PaperRecord)
    value = paper.data.object_contributions[0].quantities[0].values[0]
    assert value.value == expected["value"]
    assert value.error == expected["error"]
    assert value.unit == expected["unit"]
    assert paper.data.object_contributions[0].paper_boundness.status == "unbound"


def test_roster_to_quantities_passing_only_included_entries(space, service):
    """Value-step input comes from actual roster content; fixed values match fixtures."""
    source = import_mock_source(space, PAPER_A)
    handle = _start(space, source, request_key="a-off", expert=False, mode="synthetic")
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished"
    paper = _record(space, f"paper-fic-r05-a-{handle.execution_id}")
    contribution = paper.data.object_contributions[0]
    expected = MATERIALS["papers"]["FIC-R05-A"]["values"][0]
    assert contribution.roster_item_id == "roster-a-1"
    assert contribution.quantities[0].values[0].value == expected["value"]
    roster = _record(space, f"roster-fic-r05-a-{handle.execution_id}")
    assert contribution.item_id == roster.data.entries[0].contribution_id


def test_later_step_failure_preserves_roster_and_reuses_on_resume(space, service):
    """A value-step failure retains the roster, which recovery reuses without another
    roster call.
    """
    source = import_mock_source(space, PAPER_B)

    def fail_quantities():
        raise RuntimeError("simulated quantities failure")

    handle = _start(space, source, request_key="b-fail")
    flow_hooks = {"after_response:quantities": fail_quantities}
    from hvs_catalog.pipeline.flow import ExtractionFlow
    from hvs_catalog.pipeline.methods import DIRECT_METHOD
    from hvs_catalog.runtime import resume_execution

    failed = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(DIRECT_METHOD, mode="daily", fault_hooks=flow_hooks),
        service=service,
    )
    assert failed.outcome == "failed"
    assert any("simulated quantities failure" in error for error in failed.errors)
    roster_id = f"roster-fic-r05-b-{handle.execution_id}"
    assert space.store().current_revision(roster_id) == 1
    assert service.calls_for_step("direct_roster") == 1

    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished"
    # Reuse the roster; the value step interrupted before archiving calls once more on recovery.
    assert service.calls_for_step("direct_roster") == 1
    assert service.calls_for_step("quantities") == 2
    assert report.state == "finished"


def test_roster_step_failure_is_not_empty_success(space, service):
    """Roster-step failure fails the paper instead of returning a successful empty roster."""
    source = import_mock_source(space, PAPER_B)

    def fail_roster():
        raise RuntimeError("simulated roster failure")

    from hvs_catalog.pipeline.flow import ExtractionFlow
    from hvs_catalog.pipeline.methods import DIRECT_METHOD
    from hvs_catalog.runtime import resume_execution

    handle = _start(space, source, request_key="b-roster-fail")
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(
            DIRECT_METHOD,
            mode="daily",
            fault_hooks={"after_response:direct_roster": fail_roster},
        ),
        service=service,
    )
    assert report.outcome == "failed"
    status = get_extraction(space, handle.execution_id)
    assert status["errors"]
    assert f"roster-fic-r05-b-{handle.execution_id}" not in {
        ref["record_id"] for ref in status["result_refs"]
    }


class RogueQuantityService(PipelineMockService):
    """Malicious/faulty service injects excluded-object values into its response."""

    def __init__(self, mode):
        super().__init__()
        self.mode = mode

    def call(self, *, step, request, material):
        response = super().call(step=step, request=request, material=material)
        if step == "quantities" and self.mode == "rogue":
            spec = material_spec(material["paper_key"])
            response = dict(response)
            response["contributions"] = [
                *[item for item in response["contributions"]],
                *[
                    item
                    for item in spec["contributions"]
                    if item["roster_item_id"]
                    not in {c["roster_item_id"] for c in response["contributions"]}
                ],
            ]
        elif step == "quantities" and self.mode == "missing":
            response = dict(response)
            response["contributions"] = []
        return response


def test_quantities_rejects_excluded_or_pending_objects(space):
    """Reject excluded/unresolved values and missing included entries rather than treating
    them as successful empty output.
    """
    from hvs_catalog.pipeline import PAPER_C

    source = import_mock_source(space, PAPER_C)
    handle = _start(space, source, request_key="c-rogue", expert=False)
    report = resume_extraction(
        space, handle.execution_id, service=RogueQuantityService("rogue")
    )
    assert report.outcome == "failed"
    assert any("不接受排除或未决对象" in error for error in report.errors)

    source_b = import_mock_source(space, PAPER_B)
    handle_b = _start(space, source_b, request_key="b-missing", expert=False)
    report_b = resume_extraction(
        space, handle_b.execution_id, service=RogueQuantityService("missing")
    )
    assert report_b.outcome == "failed"
    assert any("不能当作空成功" in error for error in report_b.errors)


def test_finished_execution_rerun_is_idempotent(space, service):
    source = import_mock_source(space, PAPER_B)
    handle = _start(space, source, request_key="b-idempotent")
    first = resume_extraction(space, handle.execution_id, service=service)
    assert first.outcome == "finished"
    total_calls = len(service.calls)
    again = resume_extraction(space, handle.execution_id, service=service)
    assert again.outcome == "finished"
    assert again.mode == "already-finished"
    assert len(service.calls) == total_calls


def test_step_specific_model_reaches_service(space, service):
    """Send step-specific models through requests; uncovered fallback steps use the default
    model.
    """
    source = import_mock_source(space, PAPER_B)
    models = [
        StepModel(model="mock-default"),
        StepModel(step_id="quantities", model="mock-quantities"),
    ]
    handle = _start(space, source, request_key="b-models", models=models)
    resume_extraction(space, handle.execution_id, service=service)
    used = {call["step"]: call["model"] for call in service.calls}
    assert used == {
        "direct_roster": "mock-default",
        "quantities": "mock-quantities",
    }
