"""Single roster flow and its synthetic full-pipeline fixture registration."""

from __future__ import annotations

from .flow import ExtractionFlow
from .mock import mock_preflight
from .params import ParameterPolicy, ParameterSpec
from .registry import (
    MethodRegistration,
    MethodSpec,
    PipelineRegistry,
    StepSpec,
)

STEP_ROSTER = "roster"
STEP_QUANTITIES = "quantities"
STEP_FINALIZE = "finalize"

ROSTER_STEPS = (
    StepSpec(
        step_id=STEP_ROSTER,
        kind="roster",
        title="Roster extraction",
        description=(
            "Extract the object roster directly from the fixed material"
            " according to the inclusion rules."
        ),
    ),
    StepSpec(
        step_id=STEP_QUANTITIES,
        kind="quantities",
        title="Quantities and contributions",
        description=(
            "Extract measured values and paper contributions for included"
            " roster entries."
        ),
    ),
    StepSpec(
        step_id=STEP_FINALIZE,
        kind="finalize",
        title="Finalize objects",
        description="Link contributions to object records and close the run.",
    ),
)

MOCK_IMPLEMENTATION = "synthetic-mock"

#: Mock registration declares restricted deterministic sampling support only,
#: without arbitrary contents. Services record parameters without changing fixed responses;
#: real models declare independent trusted supported sets when integrated.
MOCK_PARAMETER_POLICY = ParameterPolicy(
    specs=(
        ParameterSpec(
            name="temperature",
            types=(float, int),
            minimum=0.0,
            maximum=2.0,
            description=(
                "Sampling temperature; recorded in requests, the deterministic"
                " mock does not change its fixed responses."
            ),
        ),
        ParameterSpec(
            name="seed",
            types=(int,),
            minimum=0,
            description=(
                "Deterministic seed; recorded in requests, the deterministic"
                " mock does not change its fixed responses."
            ),
        ),
    )
)

ROSTER_METHOD = MethodSpec(
    method_id="roster",
    steps=ROSTER_STEPS,
    implementation=MOCK_IMPLEMENTATION,
    real_extraction=False,
    version="1",
    parameter_policy=MOCK_PARAMETER_POLICY,
    model_steps=(STEP_ROSTER,),
)


def build_default_registry() -> PipelineRegistry:
    """Build a fresh trusted registry for the synthetic flow."""
    registry = PipelineRegistry()
    for spec in (ROSTER_METHOD,):
        registry.register(
            MethodRegistration(
                spec=spec,
                build_flow=lambda method_spec, mode: ExtractionFlow(
                    method_spec, mode=mode
                ),
                preflight=mock_preflight,
            )
        )
    return registry


#: Process-default registry; callers supply separate registries for isolation/extensions.
DEFAULT_REGISTRY = build_default_registry()
