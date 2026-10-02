"""受限模型参数的兼容入口；声明与校验由 llm 共用。"""

from ..llm.parameters import (  # noqa: F401
    EMPTY_PARAMETER_POLICY,
    ParameterPolicy,
    ParameterPolicyError,
    ParameterSpec,
    effective_policy,
)
