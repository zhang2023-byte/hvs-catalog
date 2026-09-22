"""合成流程：R05 用虚构材料验证共同运行与裁决接口。

科学内容固定在 ``material``；``service`` 提供账本独立的模拟模型服务；
``flow`` 是 LangGraph 流程适配器；``runner`` 提供测试与发行检查使用的
进程内入口。正式提取、裁决网页与 CLI 按后续关卡交付。
"""

from __future__ import annotations

from .flow import STEP_CONTRIBUTION, STEP_FINALIZE, STEP_ROSTER, SyntheticFlow
from .material import EXPECTED_STATUS, paper_key
from .runner import (
    OPERATOR_ID,
    Scenario,
    import_materials,
    open_service,
    pending_questions,
    run,
    start_execution,
    submit_expert_decision,
)
from .service import MODEL_NAME, MockExtractionService

__all__ = [
    "EXPECTED_STATUS",
    "MODEL_NAME",
    "OPERATOR_ID",
    "STEP_CONTRIBUTION",
    "STEP_FINALIZE",
    "STEP_ROSTER",
    "MockExtractionService",
    "Scenario",
    "SyntheticFlow",
    "import_materials",
    "open_service",
    "paper_key",
    "pending_questions",
    "run",
    "start_execution",
    "submit_expert_decision",
]
