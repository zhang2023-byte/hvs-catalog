"""Restricted model parameters: fields, types, and ranges declared by trusted registration.

Parameters are configuration, not arbitrary content containers. Trusted code
ParameterSpec declarations define support, exact types, and ranges separately for
mock/real models without forcing identical sampling fields. Allowlist validation rejects
undeclared fields, nested objects, lists, and free contents; never dynamically import
code from records/input strings. Return validated content copies for both fixed
snapshots and sent requests.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

#: Exact allowed Python parameter-value types; containers are forbidden.
_SCALAR_TYPES = (bool, int, float, str)


class ParameterPolicyError(ValueError):
    """Model parameter is unsupported or violates declared types/value constraints."""


@dataclass(frozen=True)
class ParameterSpec:
    """One supported parameter declaration with trusted-code type/range constraints."""

    name: str
    types: tuple[type, ...]
    description: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[object, ...] | None = None
    exclusive_minimum: bool = False
    exclusive_maximum: bool = False

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
            **({"choices": list(self.choices)} if self.choices is not None else {}),
            **({"exclusive_minimum": True} if self.exclusive_minimum else {}),
            **({"exclusive_maximum": True} if self.exclusive_maximum else {}),
        }


@dataclass(frozen=True)
class ParameterPolicy:
    """Complete parameter policy for a method implementation; the default empty policy
    accepts none.
    """

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
        """Validate parameters against the allowlist and return a content copy, rejecting
        undeclared contents.

        Use structural rules, not keyword blacklists: reject unsupported fields,
        containers, boolean numeric disguises, nonfinite floats, and out-of-range
        values. The new dictionary enters snapshots and ensures validated contents are
        the actual sent contents.
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
            if spec.choices is not None and value not in spec.choices:
                raise ParameterPolicyError(
                    f"{prefix}模型参数 {key!r} 不在允许的取值中：{spec.choices}"
                )
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if spec.minimum is not None and (
                    value < spec.minimum
                    or spec.exclusive_minimum
                    and value == spec.minimum
                ):
                    raise ParameterPolicyError(
                        f"{prefix}模型参数 {key!r} 低于声明的最小值"
                        f" {spec.minimum}：{value!r}"
                    )
                if spec.maximum is not None and (
                    value > spec.maximum
                    or spec.exclusive_maximum
                    and value == spec.maximum
                ):
                    raise ParameterPolicyError(
                        f"{prefix}模型参数 {key!r} 超过声明的最大值"
                        f" {spec.maximum}：{value!r}"
                    )
            normalized[key] = value
        return normalized


EMPTY_PARAMETER_POLICY = ParameterPolicy()


def effective_policy(policy: ParameterPolicy | None) -> ParameterPolicy:
    """Resolve optional policies; None means the empty policy.

    Entry and flow layers share this function so methods without declared support accept
    no model parameters on every path, without divergent semantics.
    """
    return policy if policy is not None else EMPTY_PARAMETER_POLICY
