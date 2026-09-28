"""共享提取入口：可信方法与步骤登记（C02）。

登记只绑定可信代码对象；重复方法、非法步骤边界与未知方法查询都
明确拒绝。当前只登记 synthetic-mock 实现，不宣称真实提取已可运行。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hvs_catalog.pipeline import (
    CANDIDATE_REVIEW_METHOD,
    DEFAULT_REGISTRY,
    DIRECT_METHOD,
    MethodRegistration,
    MethodRegistrationError,
    MethodSpec,
    PipelineRegistry,
    StepSpec,
    UnknownMethodError,
    build_default_registry,
    list_methods,
)
from hvs_catalog.pipeline.flow import ExtractionFlow

FIXTURES = Path(__file__).parent / "fixtures" / "pipeline"


def test_registered_methods_match_fixed_listing():
    """方法/步骤清单与固定夹具一致；方法与 step IDs 供集成接线使用。"""
    expected = json.loads((FIXTURES / "methods.json").read_text(encoding="utf-8"))
    actual = list_methods()
    assert [
        {
            "method_id": method["method_id"],
            "implementation": method["implementation"],
            "real_extraction": method["real_extraction"],
            "version": method["version"],
            "steps": [step["step_id"] for step in method["steps"]],
        }
        for method in actual
    ] == expected["methods"]
    # 共用数值与收尾步骤在两种方法中同名同界
    direct = next(m for m in actual if m["method_id"] == "direct")
    review = next(m for m in actual if m["method_id"] == "candidate_review")
    assert [s["step_id"] for s in direct["steps"]] == [
        "direct_roster",
        "quantities",
        "finalize",
    ]
    assert [s["step_id"] for s in review["steps"]] == [
        "discover_candidates",
        "review_candidates",
        "quantities",
        "finalize",
    ]
    assert {s["step_id"]: s["kind"] for s in direct["steps"]} == {
        "direct_roster": "roster",
        "quantities": "quantities",
        "finalize": "finalize",
    }


def test_mock_only_registration_does_not_claim_real_extraction():
    """登记仅标 synthetic/mock；真实 direct/candidate_review 未实现。"""
    for registration in DEFAULT_REGISTRY.list_methods():
        assert registration.spec.implementation == "synthetic-mock"
        assert registration.spec.real_extraction is False


def test_unknown_method_is_rejected():
    with pytest.raises(UnknownMethodError, match="未登记"):
        DEFAULT_REGISTRY.get("does-not-exist")


def test_duplicate_method_registration_rejected():
    registry = build_default_registry()
    with pytest.raises(MethodRegistrationError, match="不能重复"):
        registry.register(
            MethodRegistration(
                spec=DIRECT_METHOD,
                build_flow=lambda spec, mode: ExtractionFlow(spec, mode=mode),
            )
        )


def test_method_spec_requires_shared_quantities_and_finalize():
    roster = StepSpec("x_roster", "roster", "t", "d")
    quantities = StepSpec("quantities", "quantities", "t", "d")
    finalize = StepSpec("finalize", "finalize", "t", "d")
    with pytest.raises(MethodRegistrationError, match="quantities"):
        MethodSpec("m", (roster, finalize), "synthetic-mock", False, "1")
    with pytest.raises(MethodRegistrationError, match="排序"):
        MethodSpec("m", (finalize, quantities, roster), "synthetic-mock", False, "1")
    duplicate = (roster, roster, quantities, finalize)
    with pytest.raises(MethodRegistrationError, match="重复"):
        MethodSpec("m", duplicate, "synthetic-mock", False, "1")


def test_registry_binds_trusted_objects_not_strings():
    """登记绑定可信构建器对象；按标识取回同一绑定。"""
    registry = PipelineRegistry()
    built = []

    def builder(spec, *, mode):
        built.append((spec.method_id, mode))
        return ExtractionFlow(spec, mode=mode)

    registry.register(MethodRegistration(spec=DIRECT_METHOD, build_flow=builder))
    registration = registry.get("direct")
    flow = registration.build_flow(registration.spec, mode="daily")
    assert isinstance(flow, ExtractionFlow)
    assert flow.spec is DIRECT_METHOD
    assert built == [("direct", "daily")]
    # 未知方法不从输入字符串导入代码
    with pytest.raises(UnknownMethodError):
        registry.get("import os")


def test_candidate_review_spec_keeps_two_roster_steps():
    kinds = [step.kind for step in CANDIDATE_REVIEW_METHOD.steps]
    assert kinds == ["roster", "roster", "quantities", "finalize"]
    assert DIRECT_METHOD.step_ids() == ("direct_roster", "quantities", "finalize")
    assert DIRECT_METHOD.has_step("quantities")
    assert not DIRECT_METHOD.has_step("discover_candidates")
