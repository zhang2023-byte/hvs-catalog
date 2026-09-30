"""共享提取入口：受限模型参数通道与输入隔离。

``StepModel.parameters`` 只接受可信登记声明的受限字段、类型和值域；
未知参数、嵌套对象、列表、自由文本和越界值在建立执行前拒绝，拒绝
前不产生执行或服务调用副作用。合法参数原样进入固定快照与实际发送
的服务请求；构造后变异请求不能绕过，旧快照/旁路快照在恢复入口同
样核对，不改写旧数据假装安全。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hvs_catalog.pipeline import (
    DEFAULT_REGISTRY,
    PAPER_B,
    ExtractionRequest,
    MethodSpec,
    PipelineRequestError,
    StepModel,
    import_mock_source,
    resume_extraction,
    start_extraction,
    supported_parameters,
)
from hvs_catalog.pipeline.flow import ExtractionFlow
from hvs_catalog.pipeline.methods import CANDIDATE_REVIEW_METHOD, DIRECT_METHOD
from hvs_catalog.pipeline.mock import PipelineMockService
from hvs_catalog.pipeline.params import ParameterPolicy, ParameterPolicyError
from hvs_catalog.records.runs import ModelSpec
from hvs_catalog.runtime import build_input_snapshot, create_execution, resume_execution
from hvs_catalog.workspace import initialize_workspace


@pytest.fixture()
def space(tmp_path):
    workspace = initialize_workspace(tmp_path / "space")
    yield workspace
    workspace.close()


def _request(source, models, **overrides):
    payload = {
        "mode": "daily",
        "method": "direct",
        "source_refs": [source],
        "models": models,
    }
    payload.update(overrides)
    return ExtractionRequest(**payload)


@pytest.fixture()
def source(space):
    return import_mock_source(space, PAPER_B)


class SpyService(PipelineMockService):
    """记录完整请求内容的服务，用于核对实际发送的模型参数。"""

    def __init__(self):
        super().__init__()
        self.requests: list[dict] = []

    def call(self, *, step, request, material):
        self.requests.append({"step": step, "request": request})
        return super().call(step=step, request=request, material=material)


def _start_rejected(space, request, request_key="reject"):
    with pytest.raises(PipelineRequestError):
        start_extraction(space, request=request, request_key=request_key)
    assert space.store().list_executions() == []


# ---- 拒绝答案借道模型参数进入提取请求 ----


def test_nested_gold_answer_payload_rejected_before_execution(space, source):
    """审核复现：嵌套 gold_answer 参数在建立执行前拒绝。"""
    request = _request(
        source,
        models=[
            StepModel(
                model="mock-default",
                parameters={"gold_answer": {"FIC-CANDIDATE-C": "secret answer"}},
            )
        ],
    )
    service = SpyService()
    _start_rejected(space, request, request_key="nested-parameters")
    assert service.requests == []


def test_renamed_payload_rejected(space, source):
    """改名后的答案负载（payload/notes/system/prompt 等）一律拒绝。"""
    for name in ("payload", "notes", "system", "prompt", "Gold_Answer", "temperature2"):
        request = _request(
            source,
            models=[StepModel(model="mock-default", parameters={name: "secret"})],
        )
        _start_rejected(space, request, request_key=f"renamed-parameter-{name}")


def test_list_and_free_text_disguise_rejected(space, source):
    """列表、自由文本和布尔值伪装在类型层拒绝。"""
    disguises = [
        {"temperature": ["secret answer"]},
        {"temperature": {"value": 0.2}},
        {"temperature": "the answer is 520 km/s"},
        {"seed": True},
        {"seed": None},
    ]
    for parameters in disguises:
        request = _request(
            source, models=[StepModel(model="mock-default", parameters=parameters)]
        )
        _start_rejected(
            space, request, request_key=f"disguised-parameter-{id(parameters)}"
        )


def test_out_of_range_and_unknown_values_rejected(space, source):
    """声明范围内通过、越界与未知参数拒绝（非关键词拉黑）。"""
    bad = [
        {"temperature": 5.0},
        {"temperature": -0.1},
        {"seed": -1},
        {"unknown_knob": 1},
        {"": 1},
    ]
    for parameters in bad:
        request = _request(
            source, models=[StepModel(model="mock-default", parameters=parameters)]
        )
        _start_rejected(space, request, request_key=f"parameter-range-{id(parameters)}")


def test_rejection_has_no_execution_or_service_side_effects(space, source):
    """拒绝前无执行建立；反复拒绝后空间无执行行。"""
    for index in range(3):
        request = _request(
            source,
            models=[StepModel(model="m", parameters={"gold_answer": {"a": 1}})],
        )
        with pytest.raises(PipelineRequestError):
            start_extraction(
                space, request=request, request_key=f"parameter-effect-{index}"
            )
    assert space.store().list_executions() == []


# ---- 正例：合法受限参数透传到实际服务请求 ----


def test_legal_parameters_pass_through_to_service_requests(space):
    """合法参数经校验副本进入快照，实际发送内容与校验内容一致。"""
    source = import_mock_source(space, PAPER_B)
    service = SpyService()
    models = [
        StepModel(model="mock-default"),
        StepModel(
            step_id="quantities",
            model="mock-quantities",
            parameters={"temperature": 0.2, "seed": 7},
        ),
    ]
    handle = start_extraction(
        space, request=_request(source, models), request_key="allowed-parameters"
    )
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished"
    sent = {item["step"]: item["request"]["model"] for item in service.requests}
    assert sent["direct_roster"]["parameters"] == {}
    assert sent["quantities"]["parameters"] == {"temperature": 0.2, "seed": 7}
    # 快照中的模型参数与实际发送内容一致（校验后的内容就是发送内容）
    assert report.config["models"][1]["parameters"] == {
        "temperature": 0.2,
        "seed": 7,
    }


# ---- 构造后变异不能绕过 ----


def test_post_construction_mutation_rejected_at_start(space, source):
    """构造合法请求后原地变异参数字典：启动时按当前内容拒绝。"""
    model = StepModel(model="mock-default", parameters={"temperature": 0.2})
    request = _request(source, models=[model])
    model.parameters["gold_answer"] = {"FIC-CANDIDATE-C": "secret answer"}
    _start_rejected(space, request, request_key="mutated-request-at-start")
    # 整体替换为未支持参数同样拒绝
    model.parameters = {"answer": "secret"}
    _start_rejected(space, request, request_key="mutated-request-assignment")


def test_mutation_after_start_does_not_affect_frozen_execution(space, source):
    """建立执行后变异请求对象：固定快照与实际发送内容不受影响。"""
    model = StepModel(model="mock-default", parameters={"temperature": 0.2})
    request = _request(source, models=[model])
    handle = start_extraction(space, request=request, request_key="frozen-parameters")
    model.parameters["gold_answer"] = {"x": 1}
    model.parameters["temperature"] = 99.0
    service = SpyService()
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished"
    sent = [item["request"]["model"]["parameters"] for item in service.requests]
    assert sent == [{"temperature": 0.2}, {"temperature": 0.2}]
    assert report.config["models"][0]["parameters"] == {"temperature": 0.2}


# ---- 恢复入口：旧快照/旁路快照不能绕过同一策略 ----


def test_interpret_path_also_carries_only_declared_parameters(space):
    """纯 AI 解释尝试的请求同样只携带声明参数（审核指出该路径泄漏）。"""
    from hvs_catalog.pipeline import PAPER_A

    source = import_mock_source(space, PAPER_A)
    service = SpyService()
    request = _request(
        source,
        models=[StepModel(model="mock-default", parameters={"seed": 3})],
        mode="synthetic",
        expert_adjudication=False,
    )
    handle = start_extraction(
        space, request=request, request_key="parameter-interpretation"
    )
    report = resume_extraction(space, handle.execution_id, service=service)
    assert report.outcome == "finished"
    interpret = [
        item for item in service.requests if item["step"] == "interpret_ambiguity"
    ]
    assert len(interpret) == 1
    assert interpret[0]["request"]["model"]["parameters"] == {"seed": 3}
    assert "gold_answer" not in str(interpret[0]["request"])


def _legacy_execution_with_unsupported_params(space, source):
    """模拟修复前/旁路建立的执行：快照含嵌套答案参数。"""
    snapshot = build_input_snapshot(
        source_refs=[source],
        method="direct",
        models=[
            ModelSpec(
                step_id=None,
                provider=None,
                model="mock-default",
                parameters={"gold_answer": {"FIC-CANDIDATE-C": "secret answer"}},
            )
        ],
        expert_adjudication=False,
    )
    handle, _ = create_execution(
        space,
        request_key="legacy-parameters",
        input_snapshot=snapshot,
        flow=ExtractionFlow(DIRECT_METHOD, mode="daily"),
    )
    return handle


def test_resume_rejects_stored_snapshot_with_unsupported_params(space, source):
    """恢复入口核对已存模型参数；旁路快照拒绝推进且不改写旧数据。"""
    handle = _legacy_execution_with_unsupported_params(space, source)
    service = SpyService()
    with pytest.raises(PipelineRequestError, match="参数"):
        resume_extraction(space, handle.execution_id, service=service)
    assert service.requests == []
    # 不改写旧快照：存储内容保持原样（仍含未支持参数）
    stored = space.store().read_record_current(handle.run_ref.record_id)
    assert stored.data.input_snapshot.models[0].parameters == {
        "gold_answer": {"FIC-CANDIDATE-C": "secret answer"}
    }


def test_direct_runtime_resume_also_blocked_by_flow_defense(space, source):
    """直接经 runtime 恢复也不能把未校验参数发给服务（流程层防御）。"""
    handle = _legacy_execution_with_unsupported_params(space, source)
    service = SpyService()
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(DIRECT_METHOD, mode="daily"),
        service=service,
    )
    assert report.outcome == "failed"
    assert any("参数" in error for error in report.errors)
    assert service.requests == []


def test_valid_legacy_empty_parameters_still_resume(space):
    """合法旧快照（无参数）不受影响，正常恢复。"""
    source = import_mock_source(space, PAPER_B)
    request = _request(source, models=[StepModel(model="mock-default")])
    handle = start_extraction(space, request=request, request_key="empty-parameters")
    report = resume_extraction(
        space, handle.execution_id, service=PipelineMockService()
    )
    assert report.outcome == "finished"


# ---- 受限参数支持集（API 报告） ----


# ---- 未声明参数策略时拒绝流程层旁路 ----


def _undeclared_spec():
    """可信但未声明参数支持集的 MethodSpec（复用 direct 步骤以便流程可运行）。"""
    return MethodSpec(
        method_id="undeclared-direct",
        steps=DIRECT_METHOD.steps,
        implementation="custom",
        real_extraction=False,
        version="1",
        # parameter_policy 故意缺省 None
    )


def _legacy_execution(space, source, *, spec, parameters):
    """模拟旧快照/旁路建立的执行：固定快照携带任意嵌套参数。"""
    snapshot = build_input_snapshot(
        source_refs=[source],
        method=spec.method_id,
        models=[
            ModelSpec(
                step_id=None,
                provider=None,
                model="mock-default",
                parameters=parameters,
            )
        ],
        expert_adjudication=False,
    )
    handle, _ = create_execution(
        space,
        request_key=f"undeclared-policy-{spec.method_id}",
        input_snapshot=snapshot,
        flow=ExtractionFlow(spec, mode="daily"),
    )
    return handle


def test_undeclared_policy_direct_runtime_resume_blocked(space, source):
    """未声明策略 + 嵌套 payload 旧快照：直接 runtime 恢复也不得发出服务请求。"""
    spec = _undeclared_spec()
    handle = _legacy_execution(
        space,
        source,
        spec=spec,
        parameters={"payload": {"FIC-CANDIDATE-C": "secret answer"}},
    )
    service = SpyService()
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(spec, mode="daily"),
        service=service,
    )
    assert report.outcome == "failed"
    assert any("参数" in error for error in report.errors)
    assert service.requests == []
    leaked = [item for item in service.requests if "secret answer" in str(item)]
    assert leaked == []
    # 旧快照不改写：存储内容原样保留
    stored = space.store().read_record_current(handle.run_ref.record_id)
    assert stored.data.input_snapshot.models[0].parameters == {
        "payload": {"FIC-CANDIDATE-C": "secret answer"}
    }


def test_undeclared_policy_entry_resume_rejected(space, source):
    """未声明策略的执行经入口恢复：同様拒绝且无服务调用。"""
    from hvs_catalog.pipeline import MethodRegistration, PipelineRegistry

    spec = _undeclared_spec()
    handle = _legacy_execution(
        space,
        source,
        spec=spec,
        parameters={"gold_answer": {"FIC-CANDIDATE-C": "secret answer"}},
    )
    registry = PipelineRegistry()
    registry.register(
        MethodRegistration(
            spec=spec,
            build_flow=lambda method_spec, mode: ExtractionFlow(method_spec, mode=mode),
        )
    )
    service = SpyService()
    with pytest.raises(PipelineRequestError, match="参数"):
        resume_extraction(
            space, handle.execution_id, service=service, registry=registry
        )
    assert service.requests == []


def test_undeclared_policy_with_legal_empty_parameters_still_runs(space, source):
    """未声明策略 + 空参数：合法执行照常完成，不因收紧而误伤。"""
    spec = _undeclared_spec()
    handle = _legacy_execution(space, source, spec=spec, parameters={})
    service = SpyService()
    report = resume_execution(
        space,
        handle.execution_id,
        ExtractionFlow(spec, mode="daily"),
        service=service,
    )
    assert report.outcome == "finished"
    assert report.errors == []
    assert all(
        item["request"]["model"]["parameters"] == {} for item in service.requests
    )


def test_supported_parameters_describe_declared_set():
    described = supported_parameters()
    assert set(described) == {"direct", "candidate_review"}
    by_name = {item["name"]: item for item in described["direct"]}
    assert by_name["temperature"]["types"] == ["float", "int"]
    assert by_name["temperature"]["minimum"] == 0.0
    assert by_name["temperature"]["maximum"] == 2.0
    assert by_name["seed"]["types"] == ["int"]
    assert by_name["seed"]["minimum"] == 0
    assert described["candidate_review"] == described["direct"]


def test_policy_without_declaration_allows_no_parameters():
    """未声明支持集的方法（默认策略）不接受任何参数。"""
    policy = ParameterPolicy()
    with pytest.raises(ParameterPolicyError):
        policy.validate({"temperature": 0.2})
    assert policy.validate({}) == {}
    with pytest.raises(ValidationError):
        StepModel(model="m", parameters="not-a-dict")


def test_custom_registration_default_policy_is_strictest(space, source):
    """自定义登记未声明参数支持集时，带参数请求被拒绝。"""
    from hvs_catalog.pipeline import MethodRegistration, PipelineRegistry
    from hvs_catalog.pipeline.registry import StepSpec

    spec_roster = StepSpec("direct_roster", "roster", "t", "d")
    spec_quantities = StepSpec("quantities", "quantities", "t", "d")
    spec_finalize = StepSpec("finalize", "finalize", "t", "d")
    from hvs_catalog.pipeline.registry import MethodSpec

    custom = MethodSpec(
        "custom-direct",
        (spec_roster, spec_quantities, spec_finalize),
        "custom",
        False,
        "1",
    )
    registry = PipelineRegistry()
    registry.register(
        MethodRegistration(
            spec=custom,
            build_flow=lambda method_spec, mode: ExtractionFlow(method_spec, mode=mode),
        )
    )
    request = _request(
        source,
        models=[StepModel(model="m", parameters={"temperature": 0.2})],
        method="custom-direct",
    )
    with pytest.raises(PipelineRequestError, match="未声明模型参数支持集"):
        start_extraction(
            space,
            request=request,
            request_key="custom-parameter-policy",
            registry=registry,
        )
    assert DEFAULT_REGISTRY.get("direct").spec.parameter_policy is not None
    assert CANDIDATE_REVIEW_METHOD.parameter_policy is not None
