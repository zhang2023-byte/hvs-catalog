"""Registered extraction methods/common steps, synthetic-mock only.

direct produces a roster; candidate_review discovers then judges candidates. Both share
quantities for values/contributions and finalize. Real implementations await later work.
"""

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

STEP_DIRECT_ROSTER = "direct_roster"
STEP_DISCOVER_CANDIDATES = "discover_candidates"
STEP_REVIEW_CANDIDATES = "review_candidates"
STEP_QUANTITIES = "quantities"
STEP_FINALIZE = "finalize"

DIRECT_STEPS = (
    StepSpec(
        step_id=STEP_DIRECT_ROSTER,
        kind="roster",
        title="Direct roster extraction",
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

CANDIDATE_REVIEW_STEPS = (
    StepSpec(
        step_id=STEP_DISCOVER_CANDIDATES,
        kind="roster",
        title="Candidate discovery",
        description="Discover candidate objects from the fixed material.",
    ),
    StepSpec(
        step_id=STEP_REVIEW_CANDIDATES,
        kind="roster",
        title="Per-candidate review",
        description=(
            "Judge each discovered candidate for inclusion, exclusion or a"
            " pending disposition, producing the roster."
        ),
    ),
    *DIRECT_STEPS[1:],
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

DIRECT_METHOD = MethodSpec(
    method_id="direct",
    steps=DIRECT_STEPS,
    implementation=MOCK_IMPLEMENTATION,
    real_extraction=False,
    version="1",
    parameter_policy=MOCK_PARAMETER_POLICY,
)

CANDIDATE_REVIEW_METHOD = MethodSpec(
    method_id="candidate_review",
    steps=CANDIDATE_REVIEW_STEPS,
    implementation=MOCK_IMPLEMENTATION,
    real_extraction=False,
    version="1",
    parameter_policy=MOCK_PARAMETER_POLICY,
)


def build_default_registry() -> PipelineRegistry:
    """Build a fresh trusted registry for both mock methods."""
    registry = PipelineRegistry()
    for spec in (DIRECT_METHOD, CANDIDATE_REVIEW_METHOD):
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
