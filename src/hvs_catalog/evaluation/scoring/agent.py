"""辅助评分 Agent 协议、同步 mock、冻结判定存储与启用门禁。

请求只携带允许关系中的两段原报/字段、双方可定位且绑定材料的原文
片段、候选科学归属和固定输入摘要，不包含待测方法或实验组名。响应
严格枚举 equivalent/different/undecidable，必须带非空依据和指向实际
提供片段的来源引用；结构错误、空白/重复/越界/占位引用、异常或超时
一律记为 ``technical_failure``，不当作 different，也不允许新数值、
新对应 ID 或分数。材料不足时不调用适配器并保持 undecidable。

冻结保存在评分侧显式目录：条目键为完整请求（含 Gold/预测/材料/
规则/方案/程序及模型+提示+参数+预算摘要）的规范 JSON 摘要；读取和
写入都校验条目完整性（verdict/依据/引用与原始响应一致，不只是对
raw 求摘要）。发布用临时文件 + 原子排他链接：同键首次写入唯一，
同内容幂等、异内容拒绝且绝不覆盖，半写不会进入正式路径；摘要校验
用于发现假冒/改写缓存，不是对抗性身份认证。相同上下文复算复用，
包括失败与 undecidable，不重抽有利答案。

启用门禁：``protocol_only`` 仅限合成用途且只允许项目内置模拟适配器；
``production`` 在本关 fail closed——即使调用方现场构造全部通过的验证
记录也不能开启，正式验证凭据来源在 R12 可信接入前保持禁用。验证记
录模型仍实现并测试预定样例内容指纹、配置绑定与全部复测轮次 100%
一致性的规则；模拟通过只证明协议，不构成正式授权。没有
``validated=true`` 之类的逃生口。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import Field, model_validator

from hvs_catalog.records.common import Config, canonical_json, sha256_of

from .errors import AuxiliaryEnablementError, FrozenStoreError
from .models import EvidenceFragment
from .printed import Verdict

HEX64_PATTERN = r"^[0-9a-f]{64}$"


class AgentRequest(Config):
    """隔离评分进程的一次判定请求；不含待测方法或组名。

    ``left``/``right`` 是待比较的两段原报字段串；``left_fragments``/
    ``right_fragments`` 是双方绑定材料的可定位原文片段，响应引用只能
    指向这些实际提供的片段。
    """

    request_kind: Literal["printed_expression", "condition_attribution"]
    field_name: str = Field(min_length=1)
    left: str = Field(min_length=1)
    right: str = Field(min_length=1)
    left_fragments: list[EvidenceFragment] = Field(min_length=1)
    right_fragments: list[EvidenceFragment] = Field(min_length=1)
    candidate_attribution_left: str | None = None
    candidate_attribution_right: str | None = None
    context_digests: dict[str, str] = Field(min_length=1)

    @model_validator(mode="after")
    def _digest_format(self) -> AgentRequest:
        for name, digest in self.context_digests.items():
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError(f"输入摘要必须是 64 位十六进制：{name}")
        return self

    def allowed_citations(self) -> frozenset[str]:
        ids = {f"left:{fragment.fragment_id}" for fragment in self.left_fragments}
        ids.update(f"right:{fragment.fragment_id}" for fragment in self.right_fragments)
        return frozenset(ids)


class AgentResponse(Config):
    verdict: Verdict
    rationale: str = Field(min_length=1)
    source_citations: list[str] = Field(min_length=1)


class AgentAdapter(Protocol):
    """同步、可注入的判定适配器；真实联网 adapter 不在本关实现。"""

    name: str

    def judge(self, request: AgentRequest) -> AgentResponse: ...


class MockAgentAdapter:
    """protocol_only 同步 mock：仅用于合成测试，不做任何网络调用。"""

    def __init__(
        self,
        handler: Callable[[AgentRequest], Any] | None = None,
        *,
        name: str = "synthetic-mock",
    ) -> None:
        self.name = name
        self.calls: list[dict[str, object]] = []
        self._handler = handler

    def judge(self, request: AgentRequest) -> AgentResponse:
        self.calls.append(request.model_dump(mode="json"))
        if self._handler is None:
            raise RuntimeError("mock handler not configured")
        result = self._handler(request)
        if isinstance(result, AgentResponse):
            return result
        return AgentResponse.model_validate(result)


# ---------------------------------------------------------------------------
# 启用门禁
# ---------------------------------------------------------------------------


class ValidationSample(Config):
    sample_id: str = Field(min_length=1)
    relation: Literal["printed_expression", "condition_attribution"]
    left: str = Field(min_length=1)
    right: str = Field(min_length=1)
    expected_verdict: Verdict


class SampleOutcome(Config):
    sample_id: str = Field(min_length=1)
    actual_verdict: Verdict
    passed: bool


class ValidationRound(Config):
    round_id: str = Field(min_length=1)
    outcomes: list[SampleOutcome] = Field(min_length=1)

    @property
    def all_passed(self) -> bool:
        return all(outcome.passed for outcome in self.outcomes)


class AgentValidationRecord(Config):
    """预定测试集内容指纹、模型/提示/参数/预算绑定与逐轮复测结果。

    本关只用于协议一致性检查：模拟通过不能开启 production（见
    ``AuxiliaryScoringConfig.agent_allowed``），正式验证凭据接入留待
    R12 之后的可信来源。
    """

    test_set_id: str = Field(min_length=1)
    test_set_fingerprint: str = Field(pattern=HEX64_PATTERN)
    model_config_digest: str = Field(pattern=HEX64_PATTERN)
    prompt_digest: str = Field(pattern=HEX64_PATTERN)
    params_digest: str = Field(pattern=HEX64_PATTERN)
    budget_digest: str = Field(pattern=HEX64_PATTERN)
    planned_round_count: int = Field(ge=1)
    samples: list[ValidationSample] = Field(min_length=2)
    rounds: list[ValidationRound] = Field(min_length=1)

    @model_validator(mode="after")
    def _complete_and_passed(self) -> AgentValidationRecord:
        expected = {
            sample.sample_id: sample.expected_verdict for sample in self.samples
        }
        if len(expected) != len(self.samples):
            raise ValueError("验证样例编号重复")
        if (
            sha256_of([s.model_dump(mode="json") for s in self.samples])
            != self.test_set_fingerprint
        ):
            raise ValueError("测试集内容指纹与样例内容不一致")
        verdicts = set(expected.values())
        if verdicts == {"undecidable"}:
            raise ValueError("测试集必须包含预期明确的样例，不能全部预期未知")
        if "undecidable" not in verdicts:
            raise ValueError("测试集必须包含预期 undecidable 的正确拒答样例")
        if len(self.rounds) != self.planned_round_count:
            raise ValueError("复测轮次与预定轮数不一致，不能挑选成功轮次")
        for round_ in self.rounds:
            covered = [outcome.sample_id for outcome in round_.outcomes]
            if sorted(covered) != sorted(expected):
                raise ValueError(f"第 {round_.round_id} 轮必须覆盖全部预定样例")
            for outcome in round_.outcomes:
                if outcome.passed != (
                    outcome.actual_verdict == expected[outcome.sample_id]
                ):
                    raise ValueError(
                        f"第 {round_.round_id} 轮 {outcome.sample_id} 的通过标记与实际结果不符"
                    )
            if not round_.all_passed:
                raise ValueError(f"第 {round_.round_id} 轮未全部通过，不能挑选成功轮次")
        return self


class AuxiliaryScoringConfig(Config):
    """辅助判定配置：模式、模型/提示/参数/预算摘要与显式冻结目录。

    ``production`` 在本实现中 fail closed：正式能力验证与可信验证凭据
    的接入留待 R12 之后，任何现场构造的验证记录都不能开启生产调用。
    """

    mode: Literal["protocol_only", "production"] = "protocol_only"
    model_config_digest: str = Field(pattern=HEX64_PATTERN)
    prompt_digest: str = Field(pattern=HEX64_PATTERN)
    params_digest: str = Field(pattern=HEX64_PATTERN)
    budget_digest: str = Field(pattern=HEX64_PATTERN)
    store_directory: str | None = None
    validation: AgentValidationRecord | None = None

    def agent_allowed(self) -> bool:
        return self.mode == "protocol_only"

    def refusal_reason(self) -> str | None:
        if self.agent_allowed():
            return None
        return (
            "production 模式尚未接入可信正式验证凭据来源，默认拒绝"
            "（fail closed；R12 后接入获准验证记录才可开启）"
        )


def auxiliary_config_digest(config: AuxiliaryScoringConfig) -> str:
    """绑定辅助配置内容的规范 JSON 摘要；目录路径是环境，不参与摘要。"""
    payload = config.model_dump(mode="json", exclude={"store_directory"})
    return sha256_of(payload)


def protocol_only_config(
    *, store_directory: str | None = None
) -> AuxiliaryScoringConfig:
    """合成测试默认配置：protocol_only，摘要为固定合成值。"""
    return AuxiliaryScoringConfig(
        mode="protocol_only",
        model_config_digest=sha256_of({"synthetic": "model"}),
        prompt_digest=sha256_of({"synthetic": "prompt"}),
        params_digest=sha256_of({"synthetic": "params"}),
        budget_digest=sha256_of({"synthetic": "budget"}),
        store_directory=store_directory,
    )


# ---------------------------------------------------------------------------
# 冻结判定存储
# ---------------------------------------------------------------------------


class FrozenJudgment(Config):
    """一条冻结判定：绑定完整请求与响应，持久保存原始响应与错误。"""

    entry_key: str = Field(pattern=HEX64_PATTERN)
    request: dict[str, Any]
    context_digests: dict[str, str]
    response_raw: dict[str, Any] | None = None
    verdict: Verdict | None = None
    rationale: str | None = None
    source_citations: list[str] = Field(default_factory=list)
    status: Literal["ok", "technical_failure"]
    failure_reason: str | None = None
    response_digest: str | None = Field(default=None, pattern=HEX64_PATTERN)
    recorded_at: str = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent_with_raw(self) -> FrozenJudgment:
        if self.status == "ok":
            if self.verdict is None or not self.rationale:
                raise ValueError("ok 判定必须带 verdict 与非空依据")
            if self.failure_reason is not None:
                raise ValueError("ok 判定不能带失败原因")
            if not isinstance(self.response_raw, dict):
                raise ValueError("ok 判定必须保存原始响应")
            if self.response_raw.get("verdict") != self.verdict:
                raise ValueError("冻结判定的 verdict 与原始响应不一致")
            if self.response_raw.get("rationale") != self.rationale:
                raise ValueError("冻结判定的依据与原始响应不一致")
            if list(self.response_raw.get("source_citations") or []) != list(
                self.source_citations
            ):
                raise ValueError("冻结判定的来源引用与原始响应不一致")
            if not self.source_citations:
                raise ValueError("ok 判定必须带非空来源引用")
        else:
            if self.verdict is not None or self.rationale is not None:
                raise ValueError("技术失败判定不能带 verdict 或依据")
            if self.source_citations:
                raise ValueError("技术失败判定不能带来源引用")
            if self.failure_reason is None:
                raise ValueError("technical_failure 必须带失败原因")
        if (self.response_raw is None) != (self.response_digest is None):
            raise ValueError("原始响应与其摘要必须同时存在或同时缺失")
        if self.request.get("context_digests") != self.context_digests:
            raise ValueError("冻结判定的上下文摘要与请求内摘要逐项不一致")
        return self


def judgment_entry_key(request: AgentRequest) -> str:
    return sha256_of(request.model_dump(mode="json"))


def build_judgment(
    request: AgentRequest,
    *,
    response_raw: dict[str, Any] | None,
    verdict: Verdict | None,
    rationale: str | None,
    source_citations: list[str],
    status: Literal["ok", "technical_failure"],
    failure_reason: str | None,
    recorded_at: str,
) -> FrozenJudgment:
    return FrozenJudgment(
        entry_key=judgment_entry_key(request),
        request=request.model_dump(mode="json"),
        context_digests=dict(request.context_digests),
        response_raw=response_raw,
        response_digest=sha256_of(response_raw) if response_raw is not None else None,
        verdict=verdict,
        rationale=rationale,
        source_citations=source_citations,
        status=status,
        failure_reason=failure_reason,
        recorded_at=recorded_at,
    )


def _same_binding(left: FrozenJudgment, right: FrozenJudgment) -> bool:
    keys = (
        "entry_key",
        "response_digest",
        "status",
        "verdict",
        "rationale",
        "failure_reason",
    )
    return all(getattr(left, key) == getattr(right, key) for key in keys) and list(
        left.source_citations
    ) == list(right.source_citations)


class FrozenDecisionStore:
    """仅评分侧使用的显式冻结目录；原子排他发布、完整性与冲突校验。"""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, entry_key: str) -> Path:
        return self.directory / f"{entry_key}.json"

    def _read_and_verify(self, path: Path) -> FrozenJudgment:
        try:
            entry = FrozenJudgment.model_validate_json(path.read_text("utf-8"))
        except FileNotFoundError:
            raise
        except Exception as error:
            raise FrozenStoreError(
                f"冻结判定缓存损坏或被改写，保留现场不覆盖：{path.name}"
            ) from error
        if sha256_of(entry.request) != entry.entry_key:
            raise FrozenStoreError(f"冻结判定缓存请求摘要不符：{path.name}")
        if entry.response_raw is not None and entry.response_digest != sha256_of(
            entry.response_raw
        ):
            raise FrozenStoreError(f"冻结判定缓存响应被改写：{path.name}")
        return entry

    def lookup(self, entry_key: str) -> FrozenJudgment | None:
        path = self._path(entry_key)
        if not path.exists():
            return None
        return self._read_and_verify(path)

    def record(self, entry: FrozenJudgment) -> FrozenJudgment:
        if sha256_of(entry.request) != entry.entry_key:
            raise FrozenStoreError("条目键与请求内容不符，拒绝写入")
        path = self._path(entry.entry_key)
        payload = canonical_json(entry.model_dump(mode="json")) + "\n"
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # 原子排他发布：同键首次写入唯一，并发写只有一个成功。
                os.link(tmp, path)
            except FileExistsError:
                existing = self._read_and_verify(path)
                if _same_binding(existing, entry):
                    return existing
                raise FrozenStoreError(
                    f"相同上下文已有不同判定，拒绝覆盖：{entry.entry_key}"
                ) from None
        finally:
            if tmp.exists():
                tmp.unlink()
        descriptor = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return entry


# ---------------------------------------------------------------------------
# 判定客户端
# ---------------------------------------------------------------------------


@dataclass
class JudgmentUse:
    entry_key: str
    request_kind: str
    field_name: str
    verdict: Verdict | None
    status: str
    reused: bool


@dataclass
class AgentResolution:
    verdict: Verdict | None
    status: Literal["ok", "technical_failure", "insufficient_evidence"]
    entry_key: str | None


@dataclass
class AuxiliaryAdjudicator:
    """程序 undecidable 部分的唯一入口：先查冻结，再调用适配器。"""

    config: AuxiliaryScoringConfig
    adapter: AgentAdapter
    store: FrozenDecisionStore | None = None
    uses: list[JudgmentUse] = field(default_factory=list)
    _memory: dict[str, FrozenJudgment] = field(default_factory=dict)

    def __post_init__(self) -> None:
        reason = self.config.refusal_reason()
        if reason is not None:
            raise AuxiliaryEnablementError(reason)
        if self.config.mode == "protocol_only" and not isinstance(
            self.adapter, MockAgentAdapter
        ):
            raise AuxiliaryEnablementError(
                "protocol_only 仅允许项目内置 MockAgentAdapter 参加合成测试，"
                "未验证的真实适配器不得借模拟模式参加判定"
            )

    def adjudicate(self, request: AgentRequest) -> FrozenJudgment:
        key = judgment_entry_key(request)
        cached = self._memory.get(key)
        if cached is None and self.store is not None:
            cached = self.store.lookup(key)
        if cached is not None:
            self.uses.append(
                JudgmentUse(
                    entry_key=key,
                    request_kind=request.request_kind,
                    field_name=request.field_name,
                    verdict=cached.verdict,
                    status=cached.status,
                    reused=True,
                )
            )
            return cached
        entry = self._call_adapter(request)
        if self.store is not None:
            # 持久化成功（或同内容幂等返回已有赢家）后才发布内存缓存；
            # 冲突拒绝时不缓存被拒判定，后续调用读取冻结赢家或继续拒绝
            entry = self.store.record(entry)
        self._memory[key] = entry
        self.uses.append(
            JudgmentUse(
                entry_key=key,
                request_kind=request.request_kind,
                field_name=request.field_name,
                verdict=entry.verdict,
                status=entry.status,
                reused=False,
            )
        )
        return entry

    def _call_adapter(self, request: AgentRequest) -> FrozenJudgment:
        from datetime import datetime

        recorded_at = datetime.now(UTC).isoformat()
        try:
            response = self.adapter.judge(request)
            raw = response.model_dump(mode="json")
        except Exception as error:  # noqa: BLE001 - 异常/超时统一技术失败
            return build_judgment(
                request,
                response_raw=None,
                verdict=None,
                rationale=None,
                source_citations=[],
                status="technical_failure",
                failure_reason=f"adapter error: {type(error).__name__}: {error}",
                recorded_at=recorded_at,
            )
        allowed = request.allowed_citations()
        problems: list[str] = []
        if response.verdict not in {"equivalent", "different", "undecidable"}:
            problems.append("verdict 不在允许枚举内")
        if not response.rationale.strip():
            problems.append("依据为空")
        citations = list(response.source_citations)
        if not citations:
            problems.append("来源引用为空")
        if len(citations) != len(set(citations)):
            problems.append("来源引用重复")
        bad_citations = [citation for citation in citations if citation not in allowed]
        if bad_citations:
            problems.append(f"来源引用越界或占位：{bad_citations}")
        if problems:
            return build_judgment(
                request,
                response_raw=raw,
                verdict=None,
                rationale=None,
                source_citations=[],
                status="technical_failure",
                failure_reason="; ".join(problems),
                recorded_at=recorded_at,
            )
        return build_judgment(
            request,
            response_raw=raw,
            verdict=response.verdict,
            rationale=response.rationale,
            source_citations=citations,
            status="ok",
            failure_reason=None,
            recorded_at=recorded_at,
        )


class ScoringAgentResolver:
    """评分层使用的同步解析入口；仅供程序 undecidable 的关系调用。

    双侧必须提供绑定计划材料的可定位原文片段；材料不足时返回
    ``insufficient_evidence``（undecidable，不调用适配器）。
    """

    def __init__(
        self,
        adjudicator: AuxiliaryAdjudicator,
        *,
        context_digests: dict[str, str],
        materials_name: str,
        materials_fingerprint: str,
    ) -> None:
        self._adjudicator = adjudicator
        self._context_digests = dict(context_digests)
        self._materials_name = materials_name
        self._materials_fingerprint = materials_fingerprint
        self.technical_failures: list[str] = []
        self.insufficient_evidence: list[str] = []

    def _usable_fragments(
        self, fragments: list[EvidenceFragment], field_name: str
    ) -> list[EvidenceFragment] | None:
        """筛出绑定计划材料且支持当前待判字段的片段。

        绑定不符（名称/指纹/空文本/空位置）整侧不可用；与当前字段无关
        的片段被排除；两侧都没有相关片段时不发起请求（材料不足，保持
        未知）。不证明片段内容的真实科学正确性。
        """
        if not fragments:
            return None
        usable: list[EvidenceFragment] = []
        for fragment in fragments:
            if (
                fragment.material_name != self._materials_name
                or fragment.material_fingerprint != self._materials_fingerprint
                or not fragment.text.strip()
                or not fragment.location.strip()
            ):
                return None
            if fragment.supports(field_name):
                usable.append(fragment)
        if not usable:
            return None
        return sorted(usable, key=lambda fragment: fragment.fragment_id)

    def _resolve(self, request: AgentRequest) -> AgentResolution:
        entry = self._adjudicator.adjudicate(request)
        if entry.status == "technical_failure":
            self.technical_failures.append(entry.entry_key)
            return AgentResolution(
                verdict=None,
                status="technical_failure",
                entry_key=entry.entry_key,
            )
        return AgentResolution(
            verdict=entry.verdict, status="ok", entry_key=entry.entry_key
        )

    def _insufficient(self, marker: str) -> AgentResolution:
        self.insufficient_evidence.append(marker)
        return AgentResolution(
            verdict="undecidable", status="insufficient_evidence", entry_key=None
        )

    def resolve_printed(
        self,
        *,
        field_name: str,
        left: str,
        right: str,
        left_fragments: list[EvidenceFragment],
        right_fragments: list[EvidenceFragment],
        left_attribution: str | None,
        right_attribution: str | None,
    ) -> AgentResolution:
        left_usable = self._usable_fragments(left_fragments, field_name)
        right_usable = self._usable_fragments(right_fragments, field_name)
        if left_usable is None or right_usable is None:
            return self._insufficient(f"{field_name}:missing_field_evidence")
        request = AgentRequest(
            request_kind="printed_expression",
            field_name=field_name,
            left=left,
            right=right,
            left_fragments=left_usable,
            right_fragments=right_usable,
            candidate_attribution_left=left_attribution,
            candidate_attribution_right=right_attribution,
            context_digests=self._context_digests,
        )
        return self._resolve(request)

    def resolve_condition(
        self,
        *,
        left: str,
        right: str,
        left_fragments: list[EvidenceFragment],
        right_fragments: list[EvidenceFragment],
    ) -> AgentResolution:
        left_usable = self._usable_fragments(left_fragments, "condition")
        right_usable = self._usable_fragments(right_fragments, "condition")
        if left_usable is None or right_usable is None:
            return self._insufficient("condition:missing_field_evidence")
        request = AgentRequest(
            request_kind="condition_attribution",
            field_name="condition",
            left=left,
            right=right,
            left_fragments=left_usable,
            right_fragments=right_usable,
            candidate_attribution_left=left,
            candidate_attribution_right=right,
            context_digests=self._context_digests,
        )
        return self._resolve(request)
