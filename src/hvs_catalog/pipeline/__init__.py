"""Shared extraction entry points: method registration, strict requests, and execution.

Ordinary/evaluation callers share start_extraction/resume_extraction/get_extraction and
trusted list_methods registration. Full-flow demos are synthetic; prepared PDF
roster extraction uses the same entry/runtime and the real model-call service.
Extraction depends on neither scoring nor Gold; requests contain no answer paths/free
metadata.
"""

from __future__ import annotations

from .configuration import (
    ExtractionConfig,
    check_extraction_config,
    load_extraction_config,
    preview_extraction_prompt,
    start_configured_extraction,
)
from .entry import (
    ExtractionHandle,
    ExtractionReport,
    PipelineRequestError,
    execution_result_refs,
    get_extraction,
    list_methods,
    pending_questions,
    resume_extraction,
    start_extraction,
    supported_parameters,
)
from .flow import FLOW_ID, FLOW_VERSION, ExtractionFlow
from .methods import (
    DEFAULT_REGISTRY,
    MOCK_IMPLEMENTATION,
    MOCK_PARAMETER_POLICY,
    ROSTER_METHOD,
    STEP_FINALIZE,
    STEP_QUANTITIES,
    STEP_ROSTER,
    build_default_registry,
)
from .mock import (
    PAPER_A,
    PAPER_B,
    PAPER_C,
    PIPELINE_EXPERT_ID,
    SUPPORTED_PAPER_KEYS,
    MaterialBindingError,
    PipelineMockService,
    import_mock_source,
    registered_source_binding,
    resolve_registered_material,
    submit_expert_decision,
)
from .params import (
    EMPTY_PARAMETER_POLICY,
    ParameterPolicy,
    ParameterPolicyError,
    ParameterSpec,
    effective_policy,
)
from .registry import (
    MethodRegistration,
    MethodRegistrationError,
    MethodSpec,
    PipelineRegistry,
    RegistryError,
    StepSpec,
    UnknownMethodError,
)
from .request import ExtractionRequest, StepModel
from .roster import start_roster_extraction

__all__ = [
    "DEFAULT_REGISTRY",
    "EMPTY_PARAMETER_POLICY",
    "FLOW_ID",
    "FLOW_VERSION",
    "MOCK_IMPLEMENTATION",
    "MOCK_PARAMETER_POLICY",
    "PAPER_A",
    "PAPER_B",
    "PAPER_C",
    "PIPELINE_EXPERT_ID",
    "ROSTER_METHOD",
    "STEP_FINALIZE",
    "STEP_QUANTITIES",
    "STEP_ROSTER",
    "SUPPORTED_PAPER_KEYS",
    "ExtractionConfig",
    "ExtractionFlow",
    "ExtractionHandle",
    "ExtractionReport",
    "ExtractionRequest",
    "MaterialBindingError",
    "MethodRegistration",
    "MethodRegistrationError",
    "MethodSpec",
    "ParameterPolicy",
    "ParameterPolicyError",
    "ParameterSpec",
    "PipelineMockService",
    "PipelineRegistry",
    "PipelineRequestError",
    "RegistryError",
    "StepModel",
    "StepSpec",
    "UnknownMethodError",
    "build_default_registry",
    "check_extraction_config",
    "effective_policy",
    "execution_result_refs",
    "get_extraction",
    "import_mock_source",
    "list_methods",
    "load_extraction_config",
    "pending_questions",
    "preview_extraction_prompt",
    "registered_source_binding",
    "resolve_registered_material",
    "resume_extraction",
    "start_configured_extraction",
    "start_extraction",
    "start_roster_extraction",
    "submit_expert_decision",
    "supported_parameters",
]
