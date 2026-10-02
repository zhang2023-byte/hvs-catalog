"""Shared extraction entry point: trusted method/step registration (C02).

Bind trusted code objects only; explicitly reject duplicate methods, invalid step
boundaries, and unknown methods. Only synthetic-mock implementations are registered;
real extraction is not claimed runnable.
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
    """Method/step inventories match fixed fixtures; identifiers support integration
    wiring.
    """
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
    # Both methods share value/finalization step names and boundaries.
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
    """Registration marks only synthetic/mock implementations; real direct/candidate_review
    is absent.
    """
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
    """Registration binds trusted builders; identifier lookup returns the same binding."""
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
    # Never import code from an unknown method's input string.
    with pytest.raises(UnknownMethodError):
        registry.get("import os")


def test_candidate_review_spec_keeps_two_roster_steps():
    kinds = [step.kind for step in CANDIDATE_REVIEW_METHOD.steps]
    assert kinds == ["roster", "roster", "quantities", "finalize"]
    assert DIRECT_METHOD.step_ids() == ("direct_roster", "quantities", "finalize")
    assert DIRECT_METHOD.has_step("quantities")
    assert not DIRECT_METHOD.has_step("discover_candidates")
