"""Trusted code-bound extraction method/step registration and identifier lookup.

Bind trusted caller specs/builders/preflight objects only, never import/execute
record/request strings. Register implemented synthetic-mock only; real
direct/candidate_review follows later delivery without current real-extraction claims.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing references only
    from ..workspace import Workspace
    from .flow import ExtractionFlow
    from .params import ParameterPolicy
    from .request import ExtractionRequest


class RegistryError(ValueError):
    """Method registration/query violates its contract."""


class MethodRegistrationError(RegistryError):
    """Invalid registration, including duplicate methods or missing/duplicate steps."""


class UnknownMethodError(RegistryError):
    """Requested method is unregistered."""


#: Fixed boundaries: roster discovery/judgment, quantities, and finalization.
STEP_KINDS = ("roster", "quantities", "finalize")


@dataclass(frozen=True)
class StepSpec:
    """Registered step information; kind fixes its boundary."""

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
    """Roster method with ordered steps, implementation marker, and restricted parameters.

    implementation currently identifies synthetic-mock; real_extraction=False denies
    real-paper capability. Trusted parameter_policy declares fields/types/ranges; absent
    means empty, accepting none rather than arbitrary contents.
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
    """Registered specification and trusted builder/preflight.

    build_flow(spec, mode=...) returns an adapter. preflight validates requests/support
    before creation and returns issues or empty success. Both are trusted caller
    objects.
    """

    spec: MethodSpec
    build_flow: Callable[..., ExtractionFlow]
    preflight: Callable[[Workspace, ExtractionRequest], list[str]] | None = None

    def check_request(self, space: Workspace, request: ExtractionRequest) -> list[str]:
        if self.preflight is None:
            return []
        return list(self.preflight(space, request))


class PipelineRegistry:
    """Registry rejecting duplicate IDs and invalid specs."""

    def __init__(self) -> None:
        self._methods: dict[str, MethodRegistration] = {}

    def register(self, registration: MethodRegistration) -> MethodRegistration:
        spec = registration.spec
        if spec.method_id in self._methods:
            raise MethodRegistrationError(f"方法 {spec.method_id!r} 已登记，不能重复")
        # Frozen dataclass __post_init__ validates construction; registration also
        # checks overall step boundaries and parameter policies.
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
