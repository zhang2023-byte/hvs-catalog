"""提取方法与步骤的可信登记：代码内注册、按标识查询。

登记只绑定调用方传入的可信程序对象（方法规格、流程构建器与运行前
检查），不从请求或记录字符串动态导入或执行代码。当前只登记已实现
的 ``synthetic-mock`` 版本；真实 ``direct``/``candidate_review`` 提取
在后续任务交付后另行登记，本模块不宣称真实提取已可运行。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅类型引用
    from ..workspace import Workspace
    from .flow import ExtractionFlow
    from .params import ParameterPolicy
    from .request import ExtractionRequest


class RegistryError(ValueError):
    """方法登记或查询不满足约定。"""


class MethodRegistrationError(RegistryError):
    """登记内容本身不合法（重复方法、步骤缺失或重复等）。"""


class UnknownMethodError(RegistryError):
    """查询的方法标识未登记。"""


#: 步骤边界固定为三类：名单（可含发现与逐个判定）、数值与收尾。
STEP_KINDS = ("roster", "quantities", "finalize")


@dataclass(frozen=True)
class StepSpec:
    """一个提取步骤的登记信息；``kind`` 固定步骤边界。"""

    step_id: str
    kind: str
    title: str
    description: str

    def __post_init__(self) -> None:
        if not self.step_id:
            raise MethodRegistrationError("step_id 不能为空")
        if self.kind not in STEP_KINDS:
            raise MethodRegistrationError(
                f"未知步骤边界 {self.kind!r}；允许值：{STEP_KINDS}"
            )


@dataclass(frozen=True)
class MethodSpec:
    """一种名单提取方法：有序步骤、实现标记与模型参数支持集。

    ``implementation`` 标记当前登记的实现来源；当前只有
    ``synthetic-mock``。``real_extraction=False`` 表示该登记不代表真实
    论文提取已可运行。``parameter_policy`` 是该方法实现声明的受限模型
    参数支持集（字段/类型/值域）；缺省为空策略，即不接受任何模型参
    数——配置不提供随意内容容器，支持集由可信登记代码声明。
    """

    method_id: str
    steps: tuple[StepSpec, ...]
    implementation: str
    real_extraction: bool
    version: str
    parameter_policy: ParameterPolicy | None = None

    def __post_init__(self) -> None:
        if not self.method_id:
            raise MethodRegistrationError("method_id 不能为空")
        ids = [step.step_id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise MethodRegistrationError(f"方法 {self.method_id} 步骤编号重复")
        kinds = [step.kind for step in self.steps]
        if kinds.count("quantities") != 1 or kinds.count("finalize") != 1:
            raise MethodRegistrationError(
                f"方法 {self.method_id} 必须恰好包含一个 quantities 与一个"
                " finalize 步骤"
            )
        if not any(kind == "roster" for kind in kinds):
            raise MethodRegistrationError(f"方法 {self.method_id} 至少需要一个名单步骤")
        if kinds[-1] != "finalize" or kinds[-2] != "quantities":
            raise MethodRegistrationError(
                f"方法 {self.method_id} 的步骤必须以名单、共用数值与收尾排序"
            )

    def step_ids(self) -> tuple[str, ...]:
        return tuple(step.step_id for step in self.steps)

    def has_step(self, step_id: str) -> bool:
        return step_id in self.step_ids()


@dataclass(frozen=True)
class MethodRegistration:
    """登记条目：规格加上绑定的可信流程构建器与运行前检查。

    ``build_flow(spec, mode=...)`` 返回流程适配器；``preflight`` 在建立
    执行前检查请求与实现支持范围，返回问题清单（空列表表示通过）。
    两者都是调用方提供的可信对象。
    """

    spec: MethodSpec
    build_flow: Callable[..., ExtractionFlow]
    preflight: Callable[[Workspace, ExtractionRequest], list[str]] | None = None

    def check_request(self, space: Workspace, request: ExtractionRequest) -> list[str]:
        if self.preflight is None:
            return []
        return list(self.preflight(space, request))


class PipelineRegistry:
    """方法登记表；重复方法标识或非法规格拒绝登记。"""

    def __init__(self) -> None:
        self._methods: dict[str, MethodRegistration] = {}

    def register(self, registration: MethodRegistration) -> MethodRegistration:
        spec = registration.spec
        if spec.method_id in self._methods:
            raise MethodRegistrationError(f"方法 {spec.method_id!r} 已登记，不能重复")
        # 触发规格校验（frozen dataclass 的 __post_init__ 已在构造时执行，
        # 这里对登记表整体再做一次步骤边界与参数策略复核）
        MethodSpec(
            method_id=spec.method_id,
            steps=spec.steps,
            implementation=spec.implementation,
            real_extraction=spec.real_extraction,
            version=spec.version,
            parameter_policy=spec.parameter_policy,
        )
        self._methods[spec.method_id] = registration
        return registration

    def get(self, method_id: str) -> MethodRegistration:
        try:
            return self._methods[method_id]
        except KeyError:
            raise UnknownMethodError(
                f"方法 {method_id!r} 未登记；可用方法："
                + ", ".join(sorted(self._methods))
            ) from None

    def list_methods(self) -> list[MethodRegistration]:
        return [self._methods[key] for key in sorted(self._methods)]
