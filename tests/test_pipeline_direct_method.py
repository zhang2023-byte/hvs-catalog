"""共享提取入口：direct 方法的模拟执行与名单→数值传递（C02/C03）。

名单步骤产出按执行隔离的名单记录；数值步骤只接受名单中收录的条
目，被排除或未决对象进入数值响应时明确失败。后续步骤失败不抹去已
保存名单；恢复时输入相同直接复用，不再调用模拟服务。
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
    """B 篇含义明确：默认开启裁决也不产生问题，一次完成。"""
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
    """数值步骤的输入来自名单实际内容；固定值与夹具一致。"""
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
    """数值步骤失败不抹名单；恢复复用已保存名单，不再调用服务。"""
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
    # 名单复用：没有第二次名单调用；数值步骤在响应后、归档前中断，恢复重调一次
    assert service.calls_for_step("direct_roster") == 1
    assert service.calls_for_step("quantities") == 2
    assert report.state == "finished"


def test_roster_step_failure_is_not_empty_success(space, service):
    """名单步骤失败时整篇失败，不把失败当空成功名单。"""
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
    """把被排除对象的数值塞进响应的恶意/故障服务。"""

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
    """数值不接受被排除/未决对象；缺失收录条目也不是空成功。"""
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
    """逐步模型经请求传到模拟服务；fallback 未覆盖步骤用默认模型。"""
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
