"""严格提取请求：日常与评测共用入口的输入模型。

请求只接受强类型字段：模式、方法、材料引用、逐步/默认模型与专家
裁决开关，以及运行固定允许的既有决定集合。没有 Gold、评分或答案
路径参数，也不接受自由 dict 的 metadata；未知字段一律拒绝
（``extra="forbid"``），答案不能借道请求偷渡进提取。
"""

from __future__ import annotations

from pydantic import Field, model_validator

from ..records.common import Config, RecordRef
from ..records.runs import RunMode


class StepModel(Config):
    """一步的模型配置；``step_id=None`` 表示全局唯一默认（fallback）。"""

    step_id: str | None = None
    provider: str | None = None
    model: str = Field(min_length=1)
    parameters: dict[str, object] = Field(default_factory=dict)


class ExtractionRequest(Config):
    """一次共享提取执行的请求内容。

    ``source_refs`` 指向空间内已固定的来源包记录；``models`` 必须恰好
    包含一个 ``step_id=None`` 的全局默认模型，另可按步骤覆盖；步骤是
    否存在由入口按登记方法在运行前校验。``existing_decision_refs`` 是
    本运行固定允许沿用的既有决定集合（经适用性核对后使用，见架构文
    档“关闭组拒绝本轮专家决定及其衍生结果”）。
    """

    mode: RunMode
    method: str = Field(min_length=1)
    source_refs: list[RecordRef] = Field(min_length=1)
    models: list[StepModel] = Field(min_length=1)
    expert_adjudication: bool = True
    existing_decision_refs: list[RecordRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _single_default_and_unique_steps(self) -> ExtractionRequest:
        defaults = [item for item in self.models if item.step_id is None]
        if len(defaults) != 1:
            raise ValueError(
                "models 必须恰好包含一个 step_id=None 的全局默认模型"
                f"（唯一 fallback）；当前有 {len(defaults)} 个"
            )
        step_ids = [item.step_id for item in self.models if item.step_id is not None]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("同一步骤不能重复指定模型")
        return self

    def model_for(self, step_id: str) -> StepModel:
        """按步骤解析模型；无逐步覆盖时使用全局默认 fallback。"""
        default = next(item for item in self.models if item.step_id is None)
        return next((item for item in self.models if item.step_id == step_id), default)
