"""共享提取入口：方法登记、严格请求与提取执行。

日常运行与评测共用 ``start_extraction``/``resume_extraction``/``get_
extraction``；方法与步骤经可信登记表查询（``list_methods``）。当前只
登记 ``synthetic-mock`` 实现与模拟材料（见 ``mock`` 模块），不宣称真实
提取已可运行；真实 ``direct``/``candidate_review`` 留待后续任务接入。
提取侧不依赖评分或 Gold；请求没有答案路径，也不接受自由 metadata。
"""

from __future__ import annotations

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
    CANDIDATE_REVIEW_METHOD,
    DEFAULT_REGISTRY,
    DIRECT_METHOD,
    MOCK_IMPLEMENTATION,
    MOCK_PARAMETER_POLICY,
    STEP_DIRECT_ROSTER,
    STEP_DISCOVER_CANDIDATES,
    STEP_FINALIZE,
    STEP_QUANTITIES,
    STEP_REVIEW_CANDIDATES,
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

__all__ = [
    "CANDIDATE_REVIEW_METHOD",
    "DEFAULT_REGISTRY",
    "DIRECT_METHOD",
    "EMPTY_PARAMETER_POLICY",
    "FLOW_ID",
    "FLOW_VERSION",
    "MOCK_IMPLEMENTATION",
    "MOCK_PARAMETER_POLICY",
    "PAPER_A",
    "PAPER_B",
    "PAPER_C",
    "PIPELINE_EXPERT_ID",
    "STEP_DIRECT_ROSTER",
    "STEP_DISCOVER_CANDIDATES",
    "STEP_FINALIZE",
    "STEP_QUANTITIES",
    "STEP_REVIEW_CANDIDATES",
    "SUPPORTED_PAPER_KEYS",
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
    "effective_policy",
    "execution_result_refs",
    "get_extraction",
    "import_mock_source",
    "list_methods",
    "pending_questions",
    "registered_source_binding",
    "resolve_registered_material",
    "resume_extraction",
    "start_extraction",
    "submit_expert_decision",
    "supported_parameters",
]
