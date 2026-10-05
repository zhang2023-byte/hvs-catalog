"""Integrated two-group expert-review comparison and per-paper reports (C09/C10).

S11 varies only the switch for identical paper/material/method/model: enabled runs pause
for fixed simulated human decisions and resume; disabled runs stay isolated. Compute
quality through the scorer, never hardcode scores. E20 AI-only runs save model_judgments
without expert decisions and retain unresolved questions. Spy requests verify no
Gold/scoring feedback into extraction. Unknown expert time is null; wall-clock waits are
not expert work time.
"""

from __future__ import annotations

import pytest

from hvs_catalog.evaluation.adapt import prediction_from_execution
from hvs_catalog.evaluation.experiment import (
    ExperimentError,
    GroupOutcome,
    _group_summary,
    run_two_group_experiment,
)
from hvs_catalog.evaluation.scoring import gold_input_digest
from hvs_catalog.evaluation.synthetic_gold import (
    build_experiment_plan,
    synthetic_gold_for_paper,
)
from hvs_catalog.pipeline import (
    PAPER_A,
    PAPER_C,
    ExtractionRequest,
    StepModel,
    get_extraction,
    import_mock_source,
    resume_extraction,
    start_extraction,
)
from hvs_catalog.pipeline.mock import PipelineMockService
from hvs_catalog.records.questions import DecisionRecord
from hvs_catalog.workspace import initialize_workspace


@pytest.fixture()
def space(tmp_path):
    workspace = initialize_workspace(tmp_path / "space")
    yield workspace
    workspace.close()


class _SpyService(PipelineMockService):
    """Spy recording every service call to check that Gold never feeds extraction requests."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[dict] = []

    def call(self, *, step: str, request: dict, material: dict) -> dict:
        self.seen.append({"step": step, "request": request, "material": material})
        return super().call(step=step, request=request, material=material)


def _decisions_of(space, execution_id: str) -> list[DecisionRecord]:
    return [
        record
        for record in space.store().list_records()
        if isinstance(record, DecisionRecord) and record.execution_id == execution_id
    ]


def test_s11_paper_c_two_groups_computed_by_scorer(space):
    """Complete S11 comparison: only the review switch differs; actual scoring determines
    quality differences.
    """
    report = run_two_group_experiment(space, PAPER_C, method="roster")
    assert report["paper_key"] == PAPER_C
    on = report["groups"]["expert_on"]
    off = report["groups"]["expert_off"]
    # Identical material/method/model, different switch; groups have separate executions.
    assert on["method"] == off["method"] == "roster"
    assert on["models"] == off["models"]
    assert on["input_digest"] != off["input_digest"]
    assert on["execution_id"] != off["execution_id"]
    # Enabled group pauses, receives include, resumes, and extracts both objects.
    assert on["questions"] == {"created": 1, "answered": 1, "open": 0}
    assert on["decisions"]["human"] == 1
    assert on["decisions"]["simulated_choices"] == ["include"]
    assert on["roster"] == {"included": 2, "excluded": 1, "pending": 0}
    assert on["quality"]["l1"]["tp"] == 2
    assert on["quality"]["l2"]["correct"] == 2
    # Disabled unresolved entries remain unresolved without automatic exclusion or enabled-group decisions.
    assert off["questions"] == {"created": 0, "answered": 0, "open": 0}
    assert off["decisions"]["human"] == 0
    assert off["roster"] == {"included": 2, "excluded": 1, "pending": 0}
    assert off["quality"]["l1"]["tp"] == 2
    assert off["quality"]["l1"]["fn"] == 0
    assert off["quality"]["l2"]["gold"] == 2
    assert off["quality"]["l2"]["correct"] == 2
    # Isolate paper outputs by execution; disabled fixed input contains no current-round decisions.
    assert report["isolation"]["expert_off_existing_decision_refs"] == []
    assert (
        report["isolation"]["paper_record_ids"]["expert_on"]
        != report["isolation"]["paper_record_ids"]["expert_off"]
    )
    assert _decisions_of(space, off["execution_id"]) == []


def test_e20_paper_a_pure_ai_group_keeps_model_judgment(space):
    """E20 AI-only group records model judgments, unresolved questions, and no expert
    decisions.
    """
    report = run_two_group_experiment(space, PAPER_A, method="roster")
    on = report["groups"]["expert_on"]
    off = report["groups"]["expert_off"]
    assert on["questions"]["open"] == 0
    assert on["decisions"]["human"] == 1
    assert on["decisions"]["simulated_choices"] == ["no_overall_conclusion"]
    assert off["model_judgments"] == 1
    assert off["questions"]["open"] == 0
    assert off["decisions"]["human"] == 0
    assert _decisions_of(space, off["execution_id"]) == []
    # Author assessments do not affect quality scores; the scorer computes equal value quality in both groups.
    assert on["quality"]["l1"] == off["quality"]["l1"]
    assert on["quality"]["l2"] == off["quality"]["l2"]
    # AI-only author assessment remains unresolved with boundness=null, without fabricated conclusions.
    off_paper = next(
        record
        for record in space.store().list_records()
        if record.record_id == report["isolation"]["paper_record_ids"]["expert_off"]
    )
    boundness = off_paper.data.object_contributions[0].paper_boundness
    assert boundness.status == "possibly_unbound"
    assert not boundness.issue_refs


def test_report_layers_and_nulls(space):
    """Reports separate quality layers; expert time stays null and unknown usage never
    becomes zero.
    """
    report = run_two_group_experiment(space, PAPER_A, method="roster")
    for group in report["groups"].values():
        # The mock knows its token usage, so reports use integers.
        assert isinstance(group["usage"]["tokens"], int)
        # No trustworthy expert timing evidence exists; report null consistently.
        assert group["expert_time_seconds"] is None
        quality = group["quality"]
        assert set(quality) >= {"l0", "l1", "l2"}
        assert "total" not in quality and "composite" not in quality
    assert any("null" in note for note in report["notes"])
    assert any("总分" in note for note in report["notes"])


def test_group_summary_preserves_null_tokens(space):
    """Unknown tokens=null usage is reported unchanged as null."""
    source = import_mock_source(space, PAPER_A)
    handle = start_extraction(
        space,
        request=ExtractionRequest(
            mode="synthetic",
            method="roster",
            source_refs=[source],
            models=[StepModel(model="mock-default")],
            expert_adjudication=False,
        ),
        request_key="null-usage-probe",
    )
    prediction = prediction_from_execution(space, handle.execution_id)
    status = get_extraction(space, handle.execution_id)
    status["usage"] = {"attempts": 0, "tokens": None}  # Simulate unknown usage.
    group = GroupOutcome(
        switch="expert_off",
        request_key="null-usage-probe",
        handle={
            "execution_id": handle.execution_id,
            "input_digest": handle.input_digest,
            "flow_id": handle.flow_id,
            "flow_version": handle.flow_version,
            "mode": handle.mode,
            "method": handle.method,
            "models": [],
        },
        report={"outcome": "finished"},
        status=status,
        service_calls=[],
        decisions=[],
        predictions=prediction,
    )
    gold = synthetic_gold_for_paper(PAPER_A)
    summary = _group_summary(space, group, gold)
    assert summary["usage"] == {"attempts": 0, "tokens": None}
    assert summary["expert_time_seconds"] is None
    assert build_experiment_plan([PAPER_A], [gold]).paper_keys == [PAPER_A]


def test_gold_never_fed_back_to_extraction(space):
    """Spy requests verify Gold and scoring never feed extraction."""
    gold = synthetic_gold_for_paper(PAPER_C)
    gold_digest = gold_input_digest([gold])
    gold_ids = [
        value.record_id
        for obj in gold.objects
        for group in obj.quantities
        for value in group.values
    ]
    service = _SpyService()
    source = import_mock_source(space, PAPER_C)
    handle = start_extraction(
        space,
        request=ExtractionRequest(
            mode="synthetic",
            method="roster",
            source_refs=[source],
            models=[StepModel(model="mock-default")],
            expert_adjudication=False,
        ),
        request_key="no-answer-feedback",
    )
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished"
    assert service.seen, "spy 服务应记录到实际调用"
    blob = repr(service.seen)
    # Gold digests and internal locator identifiers never appear in extraction service requests.
    assert gold_digest not in blob
    for record_id in gold_ids:
        assert record_id not in blob
        assert record_id.split("-")[0] + "-rv" not in blob
    # Strict extraction requests expose no Gold/answer-path channel and reject unknown fields.
    request_payload = handle.to_dict()
    assert "gold" not in repr(request_payload).lower()


def test_daily_mode_rejected_for_experiment(space):
    with pytest.raises(ExperimentError):
        run_two_group_experiment(space, PAPER_A, mode="daily")
