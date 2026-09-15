"""模拟服务单元测试：材料快照固定合成输入、先记账后返回、重复计费。"""

import copy

import pytest

from hvs_catalog.demo import service as demo_service


@pytest.fixture()
def svc(tmp_path):
    service = demo_service.MockModelService(tmp_path / "service.db")
    yield service
    service.close()


def _material():
    return demo_service.default_material()


def _request(svc, kind, *, rules="demo-rules-v1", roster_names=None, material=None):
    material = material if material is not None else _material()
    return svc.build_request(
        kind,
        rules=rules,
        model=demo_service.MODEL_NAME,
        paper_digest=demo_service.material_digest(material),
        roster_names=roster_names,
    )


def test_call_bills_before_returning(svc):
    material = _material()
    request = _request(svc, "roster", material=material)
    call = svc.call(request, material)
    assert svc.billed_calls(request) == 1
    assert call.kind == "roster"
    assert call.cost == demo_service.STEP_PRICES["roster"]
    assert [item["name"] for item in call.response["objects"]] == [
        "J1234+5678",
        "J1234+5679",
        "J0456+7890",
    ]


def test_values_response_marks_identity_question_without_picking_values(svc):
    material = _material()
    call = svc.call(_request(svc, "values", roster_names=["a", "b", "c"]), material)
    needs = call.response["needs_decision"]
    assert needs["objects"] == ["J1234+5678", "J1234+5679"]
    # 每名候选各一条测量，全部保留来源；响应不包含任何“挑选主记录”的概念
    assert len(call.response["measurements"]) == 3
    assert {item["source"] for item in call.response["measurements"]} == {
        "当前论文",
        "引用前文",
    }


def test_response_comes_from_snapshot_not_current_globals(svc, monkeypatch):
    """响应按调用携带的材料快照生成，修改全局合成数据不影响已有快照。"""
    material = _material()
    request = _request(svc, "values", roster_names=["a", "b"], material=material)
    first = svc.call(request, material)

    mutated = copy.deepcopy(demo_service.VALUES_RESPONSE)
    mutated["measurements"][0]["value"] = 999.0
    monkeypatch.setattr(demo_service, "VALUES_RESPONSE", mutated)
    # 当前默认材料已变化（新运行的输入不同）……
    assert demo_service.material_digest(
        demo_service.default_material()
    ) != demo_service.material_digest(material)
    # ……但按原快照的调用仍返回原内容
    second = svc.call(request, material)
    assert second.response == first.response
    assert second.response["measurements"][0]["value"] == 580.0


def test_call_rejects_request_snapshot_mismatch(svc):
    material = _material()
    other = copy.deepcopy(material)
    other["values_response"]["measurements"][0]["value"] = 999.0
    mismatched = _request(svc, "roster", material=other)
    with pytest.raises(ValueError, match="材料摘要与材料快照不一致"):
        svc.call(mismatched, material)


def test_repeated_calls_bill_each_time_with_same_response(svc):
    material = _material()
    request = _request(svc, "values", roster_names=["J1234+5678", "J1234+5679"])
    first = svc.call(request, material)
    second = svc.call(request, material)
    assert svc.billed_calls(request) == 2
    assert first.response == second.response
    summary = svc.billing_summary()
    assert summary["calls"] == 2
    assert summary["total_cost"] == pytest.approx(
        2 * demo_service.STEP_PRICES["values"]
    )
    assert summary["by_step"]["values"]["calls"] == 2


def test_billing_summary_counts_by_step(svc):
    material = _material()
    svc.call(_request(svc, "roster"), material)
    request = _request(svc, "values", roster_names=["a", "b", "c"])
    svc.call(request, material)
    svc.call(request, material)
    summary = svc.billing_summary()
    assert summary["calls"] == 3
    assert summary["total_cost"] == pytest.approx(0.5 + 0.8 + 0.8)
    assert {kind: item["calls"] for kind, item in summary["by_step"].items()} == {
        "roster": 1,
        "values": 2,
    }


def test_request_digest_reflects_pinned_inputs(svc):
    material = _material()
    roster = _request(svc, "roster", material=material)
    roster_v2 = _request(svc, "roster", rules="v2", material=material)
    roster_other_model = dict(roster, model="other-model")
    other_material = copy.deepcopy(material)
    other_material["title"] = "修订版"
    roster_other_paper = _request(svc, "roster", material=other_material)
    values = _request(svc, "values", roster_names=["a"], material=material)
    digests = {
        demo_service.digest(r)
        for r in (roster, roster_v2, roster_other_model, roster_other_paper, values)
    }
    assert len(digests) == 5  # 规则、模型、材料或调用类型变化都得到不同请求


def test_unknown_kind_rejected(svc):
    with pytest.raises(ValueError, match="未知调用类型"):
        svc.build_request(
            "unknown",
            rules="v1",
            model=demo_service.MODEL_NAME,
            paper_digest=demo_service.material_digest(_material()),
        )
