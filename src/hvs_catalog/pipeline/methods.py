"""登记的提取方法与共用步骤：仅 synthetic-mock 实现。

步骤边界固定：``direct`` 一步产生名单；``candidate_review`` 先发现候
选、再逐个判定产生名单；两者之后共用 ``quantities``（数值与论文贡献，
显式承接既有 synthetic 合成流程的 ``extract_contribution`` 语义）与
``finalize``。实际的真实提取实现留待后续任务接入。
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

#: 数值步骤显式适配既有 synthetic 合成名 ``extract_contribution``。
LEGACY_CONTRIBUTION_STEP = "extract_contribution"

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
            " roster entries; explicitly adapts the synthetic"
            f" {LEGACY_CONTRIBUTION_STEP!r} step."
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

#: 已注册 mock 明确声明的模型参数支持集：仅接受受限的确定性采样配置，
#: 不提供随意内容容器。确定性模拟服务把参数记入请求但不改变固定应答；
#: 真实模型接入时由各自的可信登记另行声明支持集。
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
    """构造登记了两种 mock 方法的新登记表（可信代码内绑定）。"""
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


#: 进程内默认登记表；需要隔离或扩展时调用方传入自建登记表。
DEFAULT_REGISTRY = build_default_registry()
