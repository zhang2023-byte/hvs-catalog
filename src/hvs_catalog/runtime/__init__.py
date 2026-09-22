"""运行模块：执行创建、步骤结果与恢复。

公共入口：``create_execution``、``get_execution``、``commit_step_result``、
``resume_execution`` 及运行上下文与流程适配器基类。设计约束见
[架构设计](../../../docs/design/architecture.md#工作空间与恢复接口方案)。
"""

from __future__ import annotations

from .execution import (
    RULES_ARCHIVE_REL,
    ApplicationPlan,
    DefaultFlowAdapter,
    ExecutionHandle,
    FlowAdapter,
    RuntimeContext,
    StepRunInfo,
    append_run_revision,
    build_input_snapshot,
    commit_step_result,
    create_execution,
    current_environment,
    get_execution,
    rules_archive_rel,
    snapshot_input_digest,
    verify_execution_inputs,
    verify_flow_compatibility,
)
from .recovery import (
    AppliedDecisionInfo,
    ExecutionReport,
    resume_execution,
)

__all__ = [
    "RULES_ARCHIVE_REL",
    "ApplicationPlan",
    "AppliedDecisionInfo",
    "DefaultFlowAdapter",
    "ExecutionHandle",
    "ExecutionReport",
    "FlowAdapter",
    "RuntimeContext",
    "StepRunInfo",
    "append_run_revision",
    "build_input_snapshot",
    "commit_step_result",
    "create_execution",
    "current_environment",
    "get_execution",
    "resume_execution",
    "rules_archive_rel",
    "snapshot_input_digest",
    "verify_execution_inputs",
    "verify_flow_compatibility",
]
