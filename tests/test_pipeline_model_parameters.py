"""Shared extraction entry point: restricted model parameters and input isolation.

StepModel.parameters accepts only registered fields, types, and ranges. Reject
unknown/nested/list/free-text/out-of-range parameters before execution creation or
service calls. Valid parameters enter snapshots and sent requests unchanged. Mutated
requests cannot bypass validation; old/bypass snapshots are checked on recovery without
rewriting old data to appear safe.
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
    """Service recording complete requests to verify actually sent model parameters."""

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


# ---- Reject answers smuggled through model parameters ----


def test_nested_gold_answer_payload_rejected_before_execution(space, source):
    """Review reproducer: reject nested gold_answer before creating execution."""
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
    """Reject renamed answer payloads such as payload, notes, system, and prompt."""
    for name in ("payload", "notes", "system", "prompt", "Gold_Answer", "temperature2"):
        request = _request(
            source,
            models=[StepModel(model="mock-default", parameters={name: "secret"})],
        )
        _start_rejected(space, request, request_key=f"renamed-parameter-{name}")


def test_list_and_free_text_disguise_rejected(space, source):
    """Reject lists, free text, and boolean disguises at type validation."""
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
    """Accept declared ranges and reject out-of-range/unknown parameters, without keyword
    blacklisting.
    """
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
    """Repeated rejected requests create no executions or execution rows."""
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


# ---- Valid restricted parameters reach actual service requests ----


def test_legal_parameters_pass_through_to_service_requests(space):
    """Validated copies enter snapshots; sent parameters equal validated contents."""
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
    # Snapshot model parameters match sent contents; validated content is exactly what is sent.
    assert report.config["models"][1]["parameters"] == {
        "temperature": 0.2,
        "seed": 7,
    }


# ---- Mutation after construction cannot bypass validation ----


def test_post_construction_mutation_rejected_at_start(space, source):
    """Reject in-place mutation of a valid request's parameter dictionary at startup."""
    model = StepModel(model="mock-default", parameters={"temperature": 0.2})
    request = _request(source, models=[model])
    model.parameters["gold_answer"] = {"FIC-CANDIDATE-C": "secret answer"}
    _start_rejected(space, request, request_key="mutated-request-at-start")
    # Replacing the entire dictionary with unsupported parameters also fails.
    model.parameters = {"answer": "secret"}
    _start_rejected(space, request, request_key="mutated-request-assignment")


def test_mutation_after_start_does_not_affect_frozen_execution(space, source):
    """Request mutation after execution creation cannot affect snapshots or sent contents."""
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


# ---- Recovery enforces the same policy for legacy/bypass snapshots ----


def test_interpret_path_also_carries_only_declared_parameters(space):
    """AI-only explanation requests likewise carry declared parameters only; review
    identified leakage here.
    """
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
    """Simulate pre-fix/bypass execution whose snapshot contains nested answer parameters."""
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
    """Recovery validates stored model parameters, rejects bypass snapshots, and never
    rewrites old data.
    """
    handle = _legacy_execution_with_unsupported_params(space, source)
    service = SpyService()
    with pytest.raises(PipelineRequestError, match="参数"):
        resume_extraction(space, handle.execution_id, service=service)
    assert service.requests == []
    # Leave the legacy snapshot unchanged, including unsupported parameters.
    stored = space.store().read_record_current(handle.run_ref.record_id)
    assert stored.data.input_snapshot.models[0].parameters == {
        "gold_answer": {"FIC-CANDIDATE-C": "secret answer"}
    }


def test_direct_runtime_resume_also_blocked_by_flow_defense(space, source):
    """Direct runtime recovery cannot send unchecked parameters either; flow-level defense
    applies.
    """
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
    """Valid old snapshots without parameters recover normally."""
    source = import_mock_source(space, PAPER_B)
    request = _request(source, models=[StepModel(model="mock-default")])
    handle = start_extraction(space, request=request, request_key="empty-parameters")
    report = resume_extraction(
        space, handle.execution_id, service=PipelineMockService()
    )
    assert report.outcome == "finished"


# ---- Supported restricted parameters reported by API ----


# ---- Reject flow-level bypass when no parameter policy is declared ----


def _undeclared_spec():
    """Trusted MethodSpec without a declared parameter set, reusing direct steps to keep
    flow runnable.
    """
    return MethodSpec(
        method_id="undeclared-direct",
        steps=DIRECT_METHOD.steps,
        implementation="custom",
        real_extraction=False,
        version="1",
        # Deliberately leave parameter_policy at None.
    )


def _legacy_execution(space, source, *, spec, parameters):
    """Simulate legacy/bypass execution whose frozen snapshot carries arbitrary nested
    parameters.
    """
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
    """No declared policy plus legacy nested payload must send no service requests even
    through direct runtime recovery.
    """
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
    # Leave old snapshot storage unchanged.
    stored = space.store().read_record_current(handle.run_ref.record_id)
    assert stored.data.input_snapshot.models[0].parameters == {
        "payload": {"FIC-CANDIDATE-C": "secret answer"}
    }


def test_undeclared_policy_entry_resume_rejected(space, source):
    """Entry-point recovery without a declared policy also rejects without service calls."""
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
    """No declared policy plus empty parameters still completes normally without false
    rejection.
    """
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
    """Methods with no declared supported parameters accept none under the default policy."""
    policy = ParameterPolicy()
    with pytest.raises(ParameterPolicyError):
        policy.validate({"temperature": 0.2})
    assert policy.validate({}) == {}
    with pytest.raises(ValidationError):
        StepModel(model="m", parameters="not-a-dict")


def test_custom_registration_default_policy_is_strictest(space, source):
    """Custom registration without declared parameter support rejects parameterized
    requests.
    """
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
