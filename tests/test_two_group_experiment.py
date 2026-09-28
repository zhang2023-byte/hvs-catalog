"""集成：裁决开关两组对照实验与逐论文报告（C09/C10）。

S11：同论文同材料/方法/模型只改开关——开启组暂停→固定模拟人类决定→
恢复，关闭组隔离执行；质量差异由评分器实际计算，不硬编码分数。
E20：纯 AI 组保存 model_judgments 而不写专家决定，问题保持未决。
Gold 与评分不回灌提取：用 spy 服务核对服务请求不含任何 Gold 内容。
报告的专家用时未知为 null，不把墙钟等待当作专家操作时间。
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
    """记录全部服务调用的 spy：核对 Gold 不回灌提取服务。"""

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
    """S11 完整对照：开关是唯一差异，质量差异来自实际评分。"""
    report = run_two_group_experiment(space, PAPER_C, method="candidate_review")
    assert report["paper_key"] == PAPER_C
    on = report["groups"]["expert_on"]
    off = report["groups"]["expert_off"]
    # 同材料/方法/模型，只有开关不同；两组是不同执行
    assert on["method"] == off["method"] == "candidate_review"
    assert on["models"] == off["models"]
    assert on["input_digest"] != off["input_digest"]
    assert on["execution_id"] != off["execution_id"]
    # 开启组：候选处置暂停→include→恢复，两个对象都被提取
    assert on["questions"] == {"created": 1, "answered": 1, "open": 0}
    assert on["decisions"]["human"] == 1
    assert on["decisions"]["simulated_choices"] == ["include"]
    assert on["roster"] == {"included": 2, "excluded": 1, "pending": 0}
    assert on["quality"]["l1"]["tp"] == 2
    assert on["quality"]["l2"]["correct"] == 2
    # 关闭组：未决条目保持未决、不被自动排除，也不消费开启组决定
    assert off["questions"] == {"created": 1, "answered": 0, "open": 1}
    assert off["decisions"]["human"] == 0
    assert off["roster"] == {"included": 1, "excluded": 1, "pending": 1}
    assert off["quality"]["l1"]["tp"] == 1
    assert off["quality"]["l1"]["fn"] == 1
    assert off["quality"]["l2"]["gold"] == 2
    assert off["quality"]["l2"]["correct"] == 1
    # 两组输出按执行隔离：论文记录不同、关闭组固定输入无本轮决定
    assert report["isolation"]["expert_off_existing_decision_refs"] == []
    assert (
        report["isolation"]["paper_record_ids"]["expert_on"]
        != report["isolation"]["paper_record_ids"]["expert_off"]
    )
    assert _decisions_of(space, off["execution_id"]) == []


def test_e20_paper_a_pure_ai_group_keeps_model_judgment(space):
    """E20：纯 AI 组记录模型判断，问题未决、无专家决定。"""
    report = run_two_group_experiment(space, PAPER_A, method="direct")
    on = report["groups"]["expert_on"]
    off = report["groups"]["expert_off"]
    assert on["questions"]["open"] == 0
    assert on["decisions"]["human"] == 1
    assert on["decisions"]["simulated_choices"] == ["no_overall_conclusion"]
    assert off["model_judgments"] == 1
    assert off["questions"]["open"] == 1
    assert off["decisions"]["human"] == 0
    assert _decisions_of(space, off["execution_id"]) == []
    # 作者判断不进入质量分：两组数值质量相同（评分器实际计算）
    assert on["quality"]["l1"] == off["quality"]["l1"]
    assert on["quality"]["l2"] == off["quality"]["l2"]
    # 纯 AI 组的作者判断保持未决（boundness 为 null，不伪造结论）
    off_paper = next(
        record
        for record in space.store().list_records()
        if record.record_id == report["isolation"]["paper_record_ids"]["expert_off"]
    )
    boundness = off_paper.data.object_contributions[0].paper_boundness
    assert boundness.status is None
    assert boundness.issue_refs


def test_report_layers_and_nulls(space):
    """报告只分层报告质量；专家用时恒为 null，用量未知不伪造 0。"""
    report = run_two_group_experiment(space, PAPER_A, method="direct")
    for group in report["groups"].values():
        # 模拟服务知道自己的 token 用量：报告为整数
        assert isinstance(group["usage"]["tokens"], int)
        # 专家用时没有可信计时证据：恒为 null
        assert group["expert_time_seconds"] is None
        quality = group["quality"]
        assert set(quality) >= {"l0", "l1", "l2"}
        assert "total" not in quality and "composite" not in quality
    assert any("null" in note for note in report["notes"])
    assert any("总分" in note for note in report["notes"])


def test_group_summary_preserves_null_tokens(space):
    """用量未知（tokens=null）时报告原样写 null。"""
    source = import_mock_source(space, PAPER_A)
    handle = start_extraction(
        space,
        request=ExtractionRequest(
            mode="synthetic",
            method="direct",
            source_refs=[source],
            models=[StepModel(model="mock-default")],
            expert_adjudication=False,
        ),
        request_key="null-usage-probe",
    )
    prediction = prediction_from_execution(space, handle.execution_id)
    status = get_extraction(space, handle.execution_id)
    status["usage"] = {"attempts": 0, "tokens": None}  # 模拟未知用量
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
    """Gold 与评分不回灌提取：spy 服务请求不含任何 Gold 内容。"""
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
            method="candidate_review",
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
    # Gold 摘要与 Gold 内部定位标识绝不出现在提取服务请求里
    assert gold_digest not in blob
    for record_id in gold_ids:
        assert record_id not in blob
        assert record_id.split("-")[0] + "-rv" not in blob
    # 提取请求本身没有 Gold/答案路径通道（严格请求已拒绝未知字段）
    request_payload = handle.to_dict()
    assert "gold" not in repr(request_payload).lower()


def test_daily_mode_rejected_for_experiment(space):
    with pytest.raises(ExperimentError):
        run_two_group_experiment(space, PAPER_A, mode="daily")
