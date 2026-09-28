"""受限模型参数：由可信登记声明的字段、类型与值域。

模型参数是配置，不是随意内容容器。支持集、精确类型和数值范围只来
自代码内登记的可信 ``ParameterSpec`` 声明（当前为已注册 mock 的明确
支持集；真实模型接入时可另行声明各自的支持集，不强制所有模型同一
采样参数）。校验按允许清单进行：未声明的一律拒绝，参数值不能是嵌
套对象、列表或其他自由内容，不从任何记录或输入字符串动态导入代
码。校验通过时返回内容副本，作为固定快照与实际发送内容。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

#: 参数值允许出现的精确 Python 类型；容器类型一律不允许。
_SCALAR_TYPES = (bool, int, float, str)


class ParameterPolicyError(ValueError):
    """模型参数不在受限支持集内，或类型/值不满足声明约束。"""


@dataclass(frozen=True)
class ParameterSpec:
    """一个受支持模型参数的声明：类型与数值范围由可信代码给出。"""

    name: str
    types: tuple[type, ...]
    description: str = ""
    minimum: float | None = None
    maximum: float | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ParameterPolicyError("受支持参数的名称必须是非空字符串")
        unsupported = [t for t in self.types if t not in _SCALAR_TYPES]
        if unsupported:
            raise ParameterPolicyError(
                f"参数 {self.name!r} 声明了不允许的类型：{unsupported}；"
                "参数值只能是声明的标量类型，不能提供嵌套或自由内容容器"
            )
        if not self.types:
            raise ParameterPolicyError(f"参数 {self.name!r} 必须声明至少一个类型")

    def describe(self) -> dict:
        return {
            "name": self.name,
            "types": [t.__name__ for t in self.types],
            "minimum": self.minimum,
            "maximum": self.maximum,
            "description": self.description,
        }


@dataclass(frozen=True)
class ParameterPolicy:
    """一个方法实现接受的全部模型参数；默认（空）策略不接受任何参数。"""

    specs: tuple[ParameterSpec, ...] = field(default=())

    def __post_init__(self) -> None:
        names = [spec.name for spec in self.specs]
        if len(names) != len(set(names)):
            raise ParameterPolicyError(f"受支持参数名称重复：{names}")

    def spec_for(self, name: str) -> ParameterSpec | None:
        return next((spec for spec in self.specs if spec.name == name), None)

    def describe(self) -> list[dict]:
        return [spec.describe() for spec in self.specs]

    def validate(self, parameters: object, *, context: str = "") -> dict:
        """按允许清单校验参数并返回内容副本；任何未声明内容拒绝。

        校验不做关键词拉黑：不在声明支持集内的字段、嵌套对象、列表、
        布尔冒充数值、非有限浮点与越界值都按结构性规则拒绝。返回的新
        字典进入固定快照，保证校验后的内容就是实际发送内容。
        """
        prefix = f"{context}：" if context else ""
        if not isinstance(parameters, Mapping):
            raise ParameterPolicyError(
                f"{prefix}模型参数必须是字符串键的映射，"
                f"得到 {type(parameters).__name__}"
            )
        normalized: dict = {}
        supported = ", ".join(spec.name for spec in self.specs) or "（无）"
        for key, value in parameters.items():
            if not isinstance(key, str) or not key.strip():
                raise ParameterPolicyError(
                    f"{prefix}模型参数名必须是非空字符串，得到 {key!r}"
                )
            spec = self.spec_for(key)
            if spec is None:
                raise ParameterPolicyError(
                    f"{prefix}未支持的模型参数 {key!r}；"
                    f"当前支持：{supported}。参数通道不接受未声明内容"
                )
            if isinstance(value, (dict, list, tuple, set, frozenset, bytes, bytearray)):
                raise ParameterPolicyError(
                    f"{prefix}模型参数 {key!r} 的值不能是嵌套对象、列表或字节；"
                    "只接受声明的标量类型"
                )
            if type(value) not in spec.types:
                raise ParameterPolicyError(
                    f"{prefix}模型参数 {key!r} 需要 "
                    f"{'/'.join(t.__name__ for t in spec.types)}，"
                    f"得到 {type(value).__name__}"
                )
            if isinstance(value, float) and not math.isfinite(value):
                raise ParameterPolicyError(
                    f"{prefix}模型参数 {key!r} 的值必须是有限数，得到 {value!r}"
                )
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if spec.minimum is not None and value < spec.minimum:
                    raise ParameterPolicyError(
                        f"{prefix}模型参数 {key!r} 低于声明的最小值"
                        f" {spec.minimum}：{value!r}"
                    )
                if spec.maximum is not None and value > spec.maximum:
                    raise ParameterPolicyError(
                        f"{prefix}模型参数 {key!r} 超过声明的最大值"
                        f" {spec.maximum}：{value!r}"
                    )
            normalized[key] = value
        return normalized


EMPTY_PARAMETER_POLICY = ParameterPolicy()


def effective_policy(policy: ParameterPolicy | None) -> ParameterPolicy:
    """把可选策略统一解析为具体策略：``None`` 等同空策略。

    入口与流程层共用本函数，保证未声明支持集的方法在所有路径上都按
    “不接受任何模型参数”校验，语义不分叉。
    """
    return policy if policy is not None else EMPTY_PARAMETER_POLICY
