"""集成修复回归：失败保留名单、科学完成状态与两组失败报告（F2/F3/F7）。

- F2：数值步骤失败不丢已存名单——delivered/format_valid 按有效输出、
  completion=partial、原始错误保留；数值未产出保持空，Gold 数值全入 G；
  真正无有效结果的失败仍 failed。恢复后行为一并验证。
- F3：科学完成状态与运行图终止分开——开放/已答仍未决问题或名单未决
  条目不得报 complete；图 finished 只是运行事实。
- F7：单组或两组失败仍生成完整两组报告，分母完整、原始错误与用量
  保留，未知仍 null；不为报告成功自动裁决未知。
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
    """按步骤名注入实际服务故障：真实经运行层转为 failed 报告。"""

    def __init__(self, fail_steps: tuple[str, ...]):
        super().__init__()
        self._fail_steps = set(fail_steps)

    def call(self, *, step: str, request: dict, material: dict) -> dict:
        if step in self._fail_steps:
            raise RuntimeError(f"synthetic {step} failure")
        return super().call(step=step, request=request, material=material)


def _start(space, source, *, method="direct", expert=False, key="probe"):
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


# ---- F2：数值失败保留名单与完整分母 ----


def test_direct_quantities_failure_preserves_roster_as_partial(space):
    """direct 数值失败：已存名单保留为部分结果，Gold 数值全入 G。"""
    source = import_mock_source(space, PAPER_B)
    handle = _start(space, source, method="direct", key="f2-direct")
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
    assert obj.quantities == []  # 数值未产出保持空，不伪造
    report, prediction = _score(space, handle.execution_id, PAPER_B)
    (paper,) = report.papers
    assert paper.l0.completion_counts == {"partial": 1}
    assert paper.l1.tp == 1 and paper.l1.fn == 0  # 已确认名单照常计 L1
    assert paper.l2.correct == 0 and paper.l2.predicted == 0
    assert paper.l2.gold == 1  # 完整 G 分母


def test_candidate_review_quantities_failure_preserves_roster_as_partial(space):
    """candidate_review 同样保留名单；未决条目不算已确认 L1 对象。"""
    source = import_mock_source(space, PAPER_C)
    handle = _start(space, source, method="candidate_review", key="f2-candidate")
    failed = resume_extraction(
        space, handle.execution_id, service=_FailingStepService(("quantities",))
    )
    assert failed.outcome == "failed"
    prediction = prediction_from_execution(space, handle.execution_id)
    assert prediction.completion == "partial"
    assert prediction.delivered is True
    identifiers = [obj.identifiers for obj in prediction.objects]
    assert identifiers == [["FIC-C-1"]]  # 未决条目不是多报也不是已确认
    report, _ = _score(space, handle.execution_id, PAPER_C)
    (paper,) = report.papers
    assert paper.l1.tp == 1 and paper.l1.fn == 1
    assert paper.l2.gold == 2 and paper.l2.predicted == 0


def test_quantities_failure_recovery_completes_and_reuses_roster(space):
    """恢复用健康服务后完成；名单复用不再调用名单步骤。"""
    source = import_mock_source(space, PAPER_B)
    handle = _start(space, source, method="direct", key="f2-recover")
    resume_extraction(
        space, handle.execution_id, service=_FailingStepService(("quantities",))
    )
    healthy = PipelineMockService()
    finished = resume_extraction(space, handle.execution_id, service=healthy)
    assert finished.outcome == "finished"
    assert healthy.calls_for_step("direct_roster") == 0  # 名单结果复用
    prediction = prediction_from_execution(space, handle.execution_id)
    assert prediction.completion == "complete"
    (obj,) = prediction.objects
    assert obj.quantities and obj.quantities[0].values


def test_roster_failure_is_failed_not_empty_success(space):
    """名单步骤失败：真正无有效结果，failed 而不是空名单成功。"""
    source = import_mock_source(space, PAPER_B)
    handle = _start(space, source, method="direct", key="f2-roster-fail")
    failed = resume_extraction(
        space, handle.execution_id, service=_FailingStepService(("direct_roster",))
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


def test_candidate_review_roster_failure_is_failed(space):
    source = import_mock_source(space, PAPER_C)
    handle = _start(space, source, method="candidate_review", key="f2-cr-roster-fail")
    failed = resume_extraction(
        space,
        handle.execution_id,
        service=_FailingStepService(("review_candidates",)),
    )
    assert failed.outcome == "failed"
    prediction = prediction_from_execution(space, handle.execution_id)
    assert prediction.completion == "failed"
    assert prediction.objects == []
    report, _ = _score(space, handle.execution_id, PAPER_C)
    (paper,) = report.papers
    assert paper.l1.fn == 2 and paper.l2.gold == 2


# ---- F3：科学完成状态与图终止分离 ----


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
    # 图已终止但覆盖未完成：partial
    status = {"state": "finished", "errors": [], "questions": []}
    state, reasons = science_completion(status, roster=None, paper=paper)
    assert state == "partial"
    assert any("not_checked" in reason for reason in reasons)
    # 仍处失败：错误是现存事实；已恢复的历史错误不阻止 complete
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


def test_s11_off_group_not_complete_and_pending(space):
    """S11 C 关闭组：图 finished 但存在未决科学内容，不得报 complete。"""
    report = run_two_group_experiment(space, PAPER_C, method="candidate_review")
    off = report["groups"]["expert_off"]
    on = report["groups"]["expert_on"]
    assert off["run_state"] == "finished"
    assert off["quality"]["l0"]["completion_counts"] == {"partial": 1}
    assert off["science_completion"] == "pending"
    assert off["science_completion_reasons"]
    assert on["science_completion"] == "complete"
    assert on["quality"]["l0"]["completion_counts"] == {"complete": 1}


def test_e20_off_group_not_complete_and_pending(space):
    """E20 A 关闭组：模型判断后问题未决，L0 不得报 complete。"""
    report = run_two_group_experiment(space, PAPER_A, method="direct")
    off = report["groups"]["expert_off"]
    assert off["run_state"] == "finished"
    assert off["quality"]["l0"]["completion_counts"] == {"partial": 1}
    assert off["science_completion"] == "pending"
    assert report["groups"]["expert_on"]["science_completion"] == "complete"


# ---- F7：失败组仍生成完整两组报告 ----


def _factory(fail_on: tuple[str, ...], fail_off: tuple[str, ...]):
    def factory(switch: str, request_key: str):
        steps = fail_on if switch == "expert_on" else fail_off
        return _FailingStepService(steps)

    return factory


def test_experiment_off_group_failure_reports_both_groups(space):
    """只有关闭组数值失败：两组都在，失败组保留名单、错误与完整分母。"""
    report = run_two_group_experiment(
        space,
        PAPER_C,
        method="candidate_review",
        service_factory=_factory((), ("quantities",)),
    )
    on = report["groups"]["expert_on"]
    off = report["groups"]["expert_off"]
    assert on["run_state"] == "finished"
    assert on["science_completion"] == "complete"
    assert off["run_state"] == "failed"
    assert any("quantities failure" in error for error in off["failures"])
    assert off["quality"]["l0"]["completion_counts"] == {"partial": 1}
    assert off["quality"]["l1"]["tp"] == 1
    assert off["quality"]["l1"]["fn"] == 1
    assert off["quality"]["l2"]["gold"] == 2
    assert off["quality"]["l2"]["predicted"] == 0
    assert isinstance(off["usage"]["tokens"], int)
    assert off["expert_time_seconds"] is None
    # 未决科学内容与失败并存：pending 优先，失败事实单独保留
    assert off["science_completion"] == "pending"
    assert off["failures"]


def test_experiment_on_group_failure_reports_both_groups(space):
    """只有开启组数值失败（决定已提交）：另一组正常，两组都在。"""
    report = run_two_group_experiment(
        space,
        PAPER_C,
        method="candidate_review",
        service_factory=_factory(("quantities",), ()),
    )
    on = report["groups"]["expert_on"]
    off = report["groups"]["expert_off"]
    assert on["run_state"] == "failed"
    assert on["decisions"]["human"] == 1  # 决定事实保留
    assert any("quantities failure" in error for error in on["failures"])
    assert on["quality"]["l1"]["tp"] == 2  # include 决定后的名单已确认
    assert on["quality"]["l2"]["gold"] == 2 and on["quality"]["l2"]["predicted"] == 0
    assert off["run_state"] == "finished"


def test_experiment_both_groups_early_failure_keeps_denominators(space):
    """两组名单前失败：完整 G、全 FN、零交付，报告仍逐组生成。"""
    report = run_two_group_experiment(
        space,
        PAPER_C,
        method="candidate_review",
        service_factory=_factory(("review_candidates",), ("review_candidates",)),
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


def test_experiment_direct_method_failure_combination(space):
    """direct 方法失败组合同样出完整报告（覆盖第二种方法）。"""
    report = run_two_group_experiment(
        space,
        PAPER_B,
        method="direct",
        service_factory=_factory(("quantities",), ("direct_roster",)),
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
    """无法安全建立评价身份的组明确阻断，不出伪造质量。"""
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
            "method": "direct",
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


# ---- R3-F4：科学完成状态纳入真实名单覆盖/未列清范围/作者判断材料缺口 ----


def _healthy_records(space):
    """跑一次真实健康的执行，取回记录格式合法的名单与论文记录。"""
    from hvs_catalog.pipeline import execution_result_refs
    from hvs_catalog.records.paper import PaperRecord
    from hvs_catalog.records.roster import RosterRecord

    source = import_mock_source(space, PAPER_B)
    handle = _start(space, source, method="direct", key="r3-f4-real")
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
    """按记录格式合法字段重建记录（不写入空间，仅内存视图）。"""
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
    """R3-F4：名单覆盖 partial（含材料缺口）→ partial 并列具体原因。"""
    roster, paper = _healthy_records(space)
    assert roster.data.coverage.state == "complete"  # 基线先确认
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
    """R3-F4：未列清的原文范围按既定语义保留未决（pending）。"""
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
    """R3-F4：status=None 且仅有材料 gap_refs 的作者判断是材料缺口 partial。"""
    roster, paper = _healthy_records(space)
    dump = paper.model_dump(mode="json")
    contribution = dump["data"]["object_contributions"][0]
    assert contribution["paper_boundness"]["status"] is not None  # 基线
    contribution["paper_boundness"] = {
        "status": None,
        "evidence_refs": [],
        "issue_refs": [],  # 无科学问题：纯材料缺口
        "gap_refs": _gap_ref(roster),
    }
    from hvs_catalog.records.paper import PaperRecord

    patched = PaperRecord.model_validate(dump)
    state, reasons = science_completion(_finished_status(), roster, patched)
    assert state == "partial"
    assert any("作者总体判断" in reason and "材料" in reason for reason in reasons)


def test_boundness_with_issue_refs_stays_pending(space):
    """R3-F4：issue_refs 关联科学问题的作者判断仍按既定 pending。"""
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
    """R3-F4：真正完整、待决已解决、历史错误已恢复 → 仍 complete。"""
    roster, paper = _healthy_records(space)
    status = {
        "state": "finished",
        "errors": ["historical boom"],
        "questions": [{"status": "answered", "answered_outcome": "resolved"}],
    }
    state, reasons = science_completion(status, roster, paper)
    assert state == "complete"
    assert reasons == []
    # 未修改的真实记录基线：complete
    state, _ = science_completion(_finished_status(), roster, paper)
    assert state == "complete"


def test_l0_and_report_do_not_overstate_complete_with_real_records(space):
    """R3-F4：预测 L0 与报告层不高报 complete（partial/pending 分层保留）。"""
    roster, paper = _healthy_records(space)
    status = _finished_status()
    # 覆盖缺口：L0 partial（科学完成非 complete），不冒称 complete
    patched_roster = _patched(
        roster,
        coverage={"state": "partial", "checked_file_ids": ["paper"], "gap_refs": []},
    )
    l0 = _l0_from_status(status, patched_roster, paper)
    assert l0["delivered"] is True
    assert l0["completion"] == "partial"
    # 未列清范围：科学 pending → L0 仍 partial（评分模型无 delivered+pending）
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
    # 已交付事实与历史错误不因覆盖缺口被抹掉（分母与诊断保全）
    assert l0["delivered"] is True and l0["format_valid"] is True
    assert any("boom" in error for error in l0["errors"])
