"""Retain rosters, scientific completion state, and reports when experiment groups fail.

Value-step failures keep saved rosters: delivered/format_valid reflect valid outputs,
completion is partial, errors survive, missing values stay empty, and all Gold values
enter G. Truly absent valid results remain failed; verify recovery too. Scientific
completion differs from graph termination: unresolved questions/roster entries prevent
complete even if the graph finished. One or two failed groups still produce complete
reports with full denominators, errors, usage, and null unknowns; reporting never
automatically adjudicates unknown science.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hvs_catalog.evaluation.adapt import (
    _l0_from_status,
    prediction_from_execution,
    science_completion,
)
from hvs_catalog.evaluation.experiment import (
    GroupOutcome,
    _group_summary,
    run_two_group_experiment,
)
from hvs_catalog.evaluation.scoring import protocol_only_config, score_batch
from hvs_catalog.evaluation.synthetic_gold import (
    build_experiment_plan,
    synthetic_gold_for_paper,
)
from hvs_catalog.pipeline import (
    PAPER_A,
    PAPER_B,
    PAPER_C,
    ExtractionRequest,
    StepModel,
    import_mock_source,
    resume_extraction,
    start_extraction,
)
from hvs_catalog.pipeline.mock import PipelineMockService
from hvs_catalog.workspace import initialize_workspace


@pytest.fixture()
def space(tmp_path):
    workspace = initialize_workspace(tmp_path / "space")
    yield workspace
    workspace.close()


class _FailingStepService(PipelineMockService):
    """Inject actual service failures by step name; the runtime converts them into failed
    reports.
    """

    def __init__(self, fail_steps: tuple[str, ...]):
        super().__init__()
        self._fail_steps = set(fail_steps)

    def call(self, *, step: str, request: dict, material: dict) -> dict:
        if step in self._fail_steps:
            raise RuntimeError(f"synthetic {step} failure")
        return super().call(step=step, request=request, material=material)


def _start(space, source, *, method="roster", expert=False, key="probe"):
    return start_extraction(
        space,
        request=ExtractionRequest(
            mode="synthetic",
            method=method,
            source_refs=[source],
            models=[StepModel(model="mock-default")],
            expert_adjudication=expert,
        ),
        request_key=key,
    )


def _score(space, execution_id, paper_key):
    gold = synthetic_gold_for_paper(paper_key)
    plan = build_experiment_plan([paper_key], [gold])
    prediction = prediction_from_execution(space, execution_id)
    return score_batch(
        plan, [gold], [prediction], auxiliary=protocol_only_config()
    ), prediction


# ---- Retain rosters and full denominators after value failures ----


def test_direct_quantities_failure_preserves_roster_as_partial(space):
    """A roster value-step failure retains the roster as partial output and includes all
    Gold values in G.
    """
    source = import_mock_source(space, PAPER_B)
    handle = _start(space, source, method="roster", key="roster-quantity-failure")
    failed = resume_extraction(
        space, handle.execution_id, service=_FailingStepService(("quantities",))
    )
    assert failed.outcome == "failed"
    assert failed.state == "failed"
    prediction = prediction_from_execution(space, handle.execution_id)
    assert prediction.delivered is True
    assert prediction.format_valid is True
    assert prediction.completion == "partial"
    assert any("quantities failure" in error for error in prediction.errors)
    (obj,) = prediction.objects
    assert obj.identifiers == ["FIC-B-1"]
    assert (
        obj.quantities == []
    )  # Missing value output stays empty; do not fabricate it.
    report, prediction = _score(space, handle.execution_id, PAPER_B)
    (paper,) = report.papers
    assert paper.l0.completion_counts == {"partial": 1}
    assert (
        paper.l1.tp == 1 and paper.l1.fn == 0
    )  # Confirmed roster objects still count toward L1.
    assert paper.l2.correct == 0 and paper.l2.predicted == 0
    assert paper.l2.gold == 1  # Complete G denominator.


def test_automatic_quantities_failure_preserves_roster_as_partial(space):
    """Partial extraction also retains its roster; unresolved entries are not confirmed L1
    objects.
    """
    source = import_mock_source(space, PAPER_C)
    handle = _start(space, source, method="roster", key="candidate-quantity-failure")
    failed = resume_extraction(
        space, handle.execution_id, service=_FailingStepService(("quantities",))
    )
    assert failed.outcome == "failed"
    prediction = prediction_from_execution(space, handle.execution_id)
    assert prediction.completion == "partial"
    assert prediction.delivered is True
    identifiers = [obj.identifiers for obj in prediction.objects]
    assert identifiers == [["FIC-C-1"], ["FIC-C-3"]]
    report, _ = _score(space, handle.execution_id, PAPER_C)
    (paper,) = report.papers
    assert paper.l1.tp == 2 and paper.l1.fn == 0
    assert paper.l2.gold == 2 and paper.l2.predicted == 0


def test_quantities_failure_recovery_completes_and_reuses_roster(space):
    """Recovery with a healthy service completes execution without rerunning the saved
    roster step.
    """
    source = import_mock_source(space, PAPER_B)
    handle = _start(space, source, method="roster", key="quantity-failure-recovery")
    resume_extraction(
        space, handle.execution_id, service=_FailingStepService(("quantities",))
    )
    healthy = PipelineMockService()
    finished = resume_extraction(space, handle.execution_id, service=healthy)
    assert finished.outcome == "finished"
    assert healthy.calls_for_step("roster") == 0  # Reuse the roster result.
    prediction = prediction_from_execution(space, handle.execution_id)
    assert prediction.completion == "complete"
    (obj,) = prediction.objects
    assert obj.quantities and obj.quantities[0].values


def test_roster_failure_is_failed_not_empty_success(space):
    """Roster-step failure yields no valid output and remains failed, rather than
    successful empty output.
    """
    source = import_mock_source(space, PAPER_B)
    handle = _start(space, source, method="roster", key="roster-failure")
    failed = resume_extraction(
        space, handle.execution_id, service=_FailingStepService(("roster",))
    )
    assert failed.outcome == "failed"
    prediction = prediction_from_execution(space, handle.execution_id)
    assert prediction.delivered is False
    assert prediction.format_valid is False
    assert prediction.completion == "failed"
    assert prediction.objects == []
    assert prediction.errors
    report, _ = _score(space, handle.execution_id, PAPER_B)
    (paper,) = report.papers
    assert paper.l0.completion_counts == {"failed": 1}
    assert paper.l0.delivered == 0
    assert paper.l1.tp == 0 and paper.l1.fn == 1
    assert paper.l2.gold == 1


def test_roster_failure_keeps_full_denominator(space):
    source = import_mock_source(space, PAPER_C)
    handle = _start(space, source, method="roster", key="candidate-roster-failure")
    failed = resume_extraction(
        space,
        handle.execution_id,
        service=_FailingStepService(("roster",)),
    )
    assert failed.outcome == "failed"
    prediction = prediction_from_execution(space, handle.execution_id)
    assert prediction.completion == "failed"
    assert prediction.objects == []
    report, _ = _score(space, handle.execution_id, PAPER_C)
    (paper,) = report.papers
    assert paper.l1.fn == 2 and paper.l2.gold == 2


# ---- Separate scientific completion from graph termination ----


def _question(status, outcome=None):
    return {"status": status, "answered_outcome": outcome}


def test_science_completion_pending_beats_graph_finished():
    status = {"state": "finished", "errors": [], "questions": [_question("open")]}
    state, reasons = science_completion(status, roster=None, paper=None)
    assert state == "pending"
    assert any("开放" in reason for reason in reasons)


def test_science_completion_answered_unresolved_is_pending():
    status = {
        "state": "finished",
        "errors": [],
        "questions": [_question("answered", outcome="unresolved")],
    }
    state, _ = science_completion(status, roster=None, paper=None)
    assert state == "pending"


def test_science_completion_pending_roster_entry_is_pending():
    entry = SimpleNamespace(item_id="cand-c-3", disposition="pending")
    roster = SimpleNamespace(data=SimpleNamespace(entries=[entry]))
    status = {"state": "finished", "errors": [], "questions": []}
    state, reasons = science_completion(status, roster=roster, paper=None)
    assert state == "pending"
    assert any("未决条目" in reason for reason in reasons)


def test_science_completion_excluded_and_resolved_do_not_block_complete():
    entries = [
        SimpleNamespace(item_id="c1", disposition="included"),
        SimpleNamespace(item_id="c2", disposition="excluded"),
    ]
    roster = SimpleNamespace(data=SimpleNamespace(entries=entries))
    status = {
        "state": "finished",
        "errors": [],
        "questions": [_question("answered", outcome="resolved")],
    }
    coverage = [
        SimpleNamespace(
            quantity="observed_phase_space.radial_velocity", state="complete"
        )
    ]
    contribution = SimpleNamespace(
        item_id="c1",
        paper_boundness=SimpleNamespace(status="unbound", issue_refs=[]),
        quantity_coverage=coverage,
    )
    paper = SimpleNamespace(data=SimpleNamespace(object_contributions=[contribution]))
    state, reasons = science_completion(status, roster=roster, paper=paper)
    assert state == "complete" and reasons == []


def test_science_completion_errors_and_unfinished_are_partial():
    coverage = [
        SimpleNamespace(
            quantity="observed_phase_space.radial_velocity", state="not_checked"
        )
    ]
    contribution = SimpleNamespace(
        item_id="c1",
        paper_boundness=SimpleNamespace(status="unbound", issue_refs=[]),
        quantity_coverage=coverage,
    )
    paper = SimpleNamespace(data=SimpleNamespace(object_contributions=[contribution]))
    # A terminated graph with incomplete coverage is partial.
    status = {"state": "finished", "errors": [], "questions": []}
    state, reasons = science_completion(status, roster=None, paper=paper)
    assert state == "partial"
    assert any("not_checked" in reason for reason in reasons)
    # Current failure remains a fact; recovered historical errors do not prevent complete.
    status = {"state": "failed", "errors": ["boom"], "questions": []}
    state, reasons = science_completion(status, roster=None, paper=paper)
    assert state == "partial"
    assert any("运行错误" in reason for reason in reasons)
    assert any("failed" in reason for reason in reasons)
    status = {"state": "finished", "errors": ["historical"], "questions": []}
    complete_paper = SimpleNamespace(
        data=SimpleNamespace(
            object_contributions=[
                SimpleNamespace(
                    item_id="c1",
                    paper_boundness=SimpleNamespace(status="unbound", issue_refs=[]),
                    quantity_coverage=[
                        SimpleNamespace(
                            quantity="observed_phase_space.radial_velocity",
                            state="complete",
                        )
                    ],
                )
            ]
        )
    )
    state, reasons = science_completion(status, roster=None, paper=complete_paper)
    assert state == "complete"


def test_automatic_inclusion_completes_without_expert(space):
    """The automatic policy decides inclusion without requesting expert judgment."""
    report = run_two_group_experiment(space, PAPER_C, method="roster")
    off = report["groups"]["expert_off"]
    on = report["groups"]["expert_on"]
    assert off["run_state"] == "finished"
    assert off["quality"]["l0"]["completion_counts"] == {"complete": 1}
    assert off["science_completion"] == "complete"
    assert off["science_completion_reasons"] == []
    assert on["science_completion"] == "complete"
    assert on["quality"]["l0"]["completion_counts"] == {"complete": 1}


def test_automatic_boundness_completes_without_expert(space):
    """The automatic policy retains its interpretation as model judgment."""
    report = run_two_group_experiment(space, PAPER_A, method="roster")
    off = report["groups"]["expert_off"]
    assert off["run_state"] == "finished"
    assert off["quality"]["l0"]["completion_counts"] == {"complete": 1}
    assert off["science_completion"] == "complete"
    assert report["groups"]["expert_on"]["science_completion"] == "complete"


# ---- Failed groups still produce complete two-group reports ----


def _factory(fail_on: tuple[str, ...], fail_off: tuple[str, ...]):
    def factory(switch: str, request_key: str):
        steps = fail_on if switch == "expert_on" else fail_off
        return _FailingStepService(steps)

    return factory


def test_experiment_off_group_failure_reports_both_groups(space):
    """Only the disabled group's value step fails: retain both groups and the failed
    group's roster, errors, and full denominator.
    """
    report = run_two_group_experiment(
        space,
        PAPER_C,
        method="roster",
        service_factory=_factory((), ("quantities",)),
    )
    on = report["groups"]["expert_on"]
    off = report["groups"]["expert_off"]
    assert on["run_state"] == "finished"
    assert on["science_completion"] == "complete"
    assert off["run_state"] == "failed"
    assert any("quantities failure" in error for error in off["failures"])
    assert off["quality"]["l0"]["completion_counts"] == {"partial": 1}
    assert off["quality"]["l1"]["tp"] == 2
    assert off["quality"]["l1"]["fn"] == 0
    assert off["quality"]["l2"]["gold"] == 2
    assert off["quality"]["l2"]["predicted"] == 0
    assert isinstance(off["usage"]["tokens"], int)
    assert off["expert_time_seconds"] is None
    # A failed quantity step retains the completed roster as partial output.
    assert off["science_completion"] == "partial"
    assert off["failures"]


def test_experiment_on_group_failure_reports_both_groups(space):
    """Only the enabled group's value step fails after decision submission: both groups
    remain, and the other succeeds.
    """
    report = run_two_group_experiment(
        space,
        PAPER_C,
        method="roster",
        service_factory=_factory(("quantities",), ()),
    )
    on = report["groups"]["expert_on"]
    off = report["groups"]["expert_off"]
    assert on["run_state"] == "failed"
    assert on["decisions"]["human"] == 1  # Retain the decision fact.
    assert any("quantities failure" in error for error in on["failures"])
    assert (
        on["quality"]["l1"]["tp"] == 2
    )  # The roster is confirmed after an include decision.
    assert on["quality"]["l2"]["gold"] == 2 and on["quality"]["l2"]["predicted"] == 0
    assert off["run_state"] == "finished"


def test_experiment_both_groups_early_failure_keeps_denominators(space):
    """Both groups fail before the roster: retain complete G, all false negatives, zero
    delivery, and per-group reports.
    """
    report = run_two_group_experiment(
        space,
        PAPER_C,
        method="roster",
        service_factory=_factory(("roster",), ("roster",)),
    )
    for name, group in report["groups"].items():
        assert group["run_state"] == "failed", name
        assert group["failures"], name
        assert group["quality"]["l0"]["delivered"] == 0
        assert group["quality"]["l0"]["completion_counts"] == {"failed": 1}
        assert group["quality"]["l1"]["fn"] == 2
        assert group["quality"]["l2"]["gold"] == 2
        assert group["quality"]["l2"]["predicted"] == 0
        assert group["usage"]["tokens"] is not None
        assert group["science_completion"] == "failed"
    assert report["groups"]["expert_on"]["decisions"]["human"] == 0


def test_experiment_roster_failure_combination(space):
    """Roster failures at different steps still produce complete group reports."""
    report = run_two_group_experiment(
        space,
        PAPER_B,
        method="roster",
        service_factory=_factory(("quantities",), ("roster",)),
    )
    on = report["groups"]["expert_on"]
    off = report["groups"]["expert_off"]
    assert on["run_state"] == "failed"
    assert on["quality"]["l0"]["completion_counts"] == {"partial": 1}
    assert on["quality"]["l1"]["tp"] == 1
    assert off["run_state"] == "failed"
    assert off["quality"]["l0"]["completion_counts"] == {"failed": 1}
    assert off["quality"]["l1"]["fn"] == 1


def test_group_summary_reports_blocked_reason_without_quality(space):
    """Block groups whose evaluation identity cannot safely be established instead of
    fabricating quality results.
    """
    gold = synthetic_gold_for_paper(PAPER_B)
    group = GroupOutcome(
        switch="expert_on",
        request_key="blocked-probe",
        handle={
            "execution_id": "exec-missing",
            "input_digest": "0" * 64,
            "flow_id": "hvs-extraction-synthetic",
            "flow_version": "1",
            "mode": "synthetic",
            "method": "roster",
            "models": [],
        },
        report={"outcome": "failed"},
        status={
            "state": "failed",
            "errors": ["identity lost"],
            "questions": [],
            "model_judgments": [],
            "usage": {"attempts": 0, "tokens": None},
            "config": {"existing_decision_refs": []},
            "steps": [],
        },
        service_calls=[],
        decisions=[],
        predictions=None,
        blocked_reason="预测适配失败：执行没有任何可定位论文的名单或论文记录",
    )
    summary = _group_summary(space, group, gold)
    assert summary["blocked_reason"]
    assert summary["quality"] is None
    assert summary["usage"] == {"attempts": 0, "tokens": None}
    assert summary["failures"] == ["identity lost"]


# ---- Scientific completion includes roster coverage, unlisted ranges, and author-assessment material gaps ----


def _healthy_records(space):
    """Run one actual healthy execution and retrieve structurally valid roster and paper
    records.
    """
    from hvs_catalog.pipeline import execution_result_refs
    from hvs_catalog.records.paper import PaperRecord
    from hvs_catalog.records.roster import RosterRecord

    source = import_mock_source(space, PAPER_B)
    handle = _start(space, source, method="roster", key="bound-source-completion")
    finished = resume_extraction(
        space, handle.execution_id, service=PipelineMockService()
    )
    assert finished.outcome == "finished"
    roster = None
    paper = None
    for ref in execution_result_refs(space, handle.execution_id):
        record = space.store().read_record_current(ref.record_id)
        if isinstance(record, RosterRecord) and roster is None:
            roster = record
        elif isinstance(record, PaperRecord) and paper is None:
            paper = record
    assert roster is not None and paper is not None
    return roster, paper


def _patched(record, **data_overrides):
    """Rebuild records through valid fields as in-memory views without writing to the
    workspace.
    """
    from hvs_catalog.records.paper import PaperRecord
    from hvs_catalog.records.roster import RosterRecord

    dump = record.model_dump(mode="json")
    dump["data"] = dump["data"] | data_overrides
    model = PaperRecord if isinstance(record, PaperRecord) else RosterRecord
    return model.model_validate(dump)


def _gap_ref(roster):
    return [
        {
            "source_ref": roster.data.source_ref.model_dump(mode="json"),
            "gap_id": "gap-table-3",
        }
    ]


def _finished_status(errors=("historical boom",)):
    return {
        "state": "finished",
        "errors": list(errors),
        "questions": [],
    }


def test_roster_coverage_partial_is_partial_with_reasons(space):
    """Partial roster coverage with material gaps yields partial completion and specific
    reasons.
    """
    roster, paper = _healthy_records(space)
    assert roster.data.coverage.state == "complete"  # Confirm the baseline first.
    patched = _patched(
        roster,
        coverage={
            "state": "partial",
            "checked_file_ids": ["paper"],
            "gap_refs": _gap_ref(roster),
        },
    )
    state, reasons = science_completion(_finished_status(), patched, paper)
    assert state == "partial"
    assert any("覆盖" in reason and "partial" in reason for reason in reasons)
    assert any("材料缺口" in reason for reason in reasons)


def test_roster_unresolved_groups_are_pending(space):
    """An unlisted original range remains pending under the established semantics."""
    roster, paper = _healthy_records(space)
    patched = _patched(
        roster,
        unresolved_groups=[
            {
                "group_id": "range-table-4",
                "original_range": "Table 4, rows 1-20",
                "evidence_refs": [roster.data.source_ref.model_dump(mode="json")],
                "known_member_item_ids": [],
                "reason": "The table lists identifiers without disambiguating members.",
            }
        ],
    )
    state, reasons = science_completion(_finished_status(), patched, paper)
    assert state == "pending"
    assert any("未列清" in reason for reason in reasons)


def test_boundness_gap_refs_only_is_partial(space):
    """An author assessment with status=None and only gap_refs is a material gap and
    remains partial.
    """
    roster, paper = _healthy_records(space)
    dump = paper.model_dump(mode="json")
    contribution = dump["data"]["object_contributions"][0]
    assert contribution["paper_boundness"]["status"] is not None  # Baseline.
    contribution["paper_boundness"] = {
        "status": None,
        "evidence_refs": [],
        "issue_refs": [],  # No scientific question: only a material gap.
        "gap_refs": _gap_ref(roster),
    }
    from hvs_catalog.records.paper import PaperRecord

    patched = PaperRecord.model_validate(dump)
    state, reasons = science_completion(_finished_status(), roster, patched)
    assert state == "partial"
    assert any("作者总体判断" in reason and "材料" in reason for reason in reasons)


def test_boundness_with_issue_refs_stays_pending(space):
    """An author assessment linked to scientific questions by issue_refs remains pending."""
    roster, paper = _healthy_records(space)
    dump = paper.model_dump(mode="json")
    contribution = dump["data"]["object_contributions"][0]
    contribution["paper_boundness"] = {
        "status": None,
        "evidence_refs": [],
        "issue_refs": [roster.data.source_ref.model_dump(mode="json")],
        "gap_refs": [],
    }
    from hvs_catalog.records.paper import PaperRecord

    patched = PaperRecord.model_validate(dump)
    state, reasons = science_completion(_finished_status(), roster, patched)
    assert state == "pending"
    assert any("作者总体判断" in reason for reason in reasons)


def test_clean_records_with_resolved_question_stay_complete(space):
    """Truly complete results with resolved pending items and recovered historical errors
    remain complete.
    """
    roster, paper = _healthy_records(space)
    status = {
        "state": "finished",
        "errors": ["historical boom"],
        "questions": [{"status": "answered", "answered_outcome": "resolved"}],
    }
    state, reasons = science_completion(status, roster, paper)
    assert state == "complete"
    assert reasons == []
    # Unmodified real-record baseline: complete.
    state, _ = science_completion(_finished_status(), roster, paper)
    assert state == "complete"


def test_l0_and_report_do_not_overstate_complete_with_real_records(space):
    """Prediction L0 and reports never overstate completion; retain partial/pending
    distinctions.
    """
    roster, paper = _healthy_records(space)
    status = _finished_status()
    # Coverage gaps make L0 partial; do not claim scientific completion.
    patched_roster = _patched(
        roster,
        coverage={"state": "partial", "checked_file_ids": ["paper"], "gap_refs": []},
    )
    l0 = _l0_from_status(status, patched_roster, paper)
    assert l0["delivered"] is True
    assert l0["completion"] == "partial"
    # Unlisted ranges are scientifically pending but L0 partial; the scoring model has no delivered+pending state.
    pending_roster = _patched(
        roster,
        unresolved_groups=[
            {
                "group_id": "range-table-4",
                "original_range": "Table 4, rows 1-20",
                "evidence_refs": [roster.data.source_ref.model_dump(mode="json")],
                "known_member_item_ids": [],
                "reason": "Unenumerated.",
            }
        ],
    )
    l0 = _l0_from_status(status, pending_roster, paper)
    assert l0["completion"] == "partial"
    state, _ = science_completion(status, pending_roster, paper)
    assert state == "pending"
    # Coverage gaps do not erase delivery facts or historical errors; preserve denominators and diagnostics.
    assert l0["delivered"] is True and l0["format_valid"] is True
    assert any("boom" in error for error in l0["errors"])
