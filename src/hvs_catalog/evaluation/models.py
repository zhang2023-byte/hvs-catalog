"""Typed Gold annotations with independent annotation metadata and PDF evidence.

Reuse shared scientific vocabularies and legacy numeric semantics: preserve verbatim,
without conversions/default completion. Expert PDF evidence retains fixed file
references, original location descriptions, and optional file page ordinals/quotes.
draft permits missing official-answer fields with explicit pending items;
checking/approval gates answer. Models enforce structure only. Execution IDs, framework
progress, and automatic extraction methods do not belong here.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from ..records.common import Config, canonical_json
from ..records.rules import author_state_ids, quantity_paths
from .errors import GoldFormatError
from .legacy import LEGACY_AUTHOR_STATUSES
from .numbers import (
    COORDINATE_QUANTITY_RANGES,
    parse_plain_number,
    parse_probability,
    parse_sexagesimal,
)

GOLD_SCHEMA_NAME = "evaluation.hvs_contribution_gold"
GOLD_SCHEMA_VERSION = 1

GOLD_LIMIT_KINDS = ("none", "lower_limit", "upper_limit", "range")
# Match shared IntervalKind vocabulary, including unspecified for absent interval interpretation.
GOLD_INTERVAL_KINDS = (
    "ordinary_range",
    "prior_support",
    "confidence",
    "credible",
    "statistical_interval",
    "unspecified",
)
GOLD_REPRESENTATIONS = ("linear", "log10", "distance_modulus")
GOLD_COORDINATE_FORMATS = (
    "decimal_degrees",
    "sexagesimal_hms",
    "sexagesimal_dms",
    "sexagesimal_colon",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class GoldSchemaTag(Config):
    name: str = Field(min_length=1)
    version: int = Field(ge=1)

    @model_validator(mode="after")
    def known_schema(self) -> GoldSchemaTag:
        if self.name != GOLD_SCHEMA_NAME:
            raise GoldFormatError(
                f"Gold 文档的 schema.name 不是 {GOLD_SCHEMA_NAME!r}：{self.name!r}"
            )
        if self.version != GOLD_SCHEMA_VERSION:
            raise GoldFormatError(
                f"未知 Gold 格式版本：v{self.version}"
                f"（当前支持 {GOLD_SCHEMA_NAME} v{GOLD_SCHEMA_VERSION}）"
            )
        return self


class GoldRulesRef(Config):
    """Scientific rules used for the original annotation; current scoring checks compatibility."""

    version: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256_RE)


class GoldPaper(Config):
    paper_key: str = Field(min_length=1)
    arxiv_id: str | None
    version: str | None = Field(default=None, min_length=1)


class GoldActor(Config):
    """Declared maker identity; AI identity never constitutes expert approval."""

    actor_id: str = Field(min_length=1, pattern=r"\S")
    kind: Literal["human", "ai"]
    model: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def actor_shape(self) -> GoldActor:
        if self.kind == "ai" and not self.model:
            raise GoldFormatError("AI 制作者必须记录模型")
        if self.kind == "human" and self.model is not None:
            raise GoldFormatError("人类身份不填写模型")
        return self


class GoldHuman(Config):
    """Declared human expert identity; no authentication is implied."""

    actor_id: str = Field(min_length=1, pattern=r"\S")
    kind: Literal["human"] = "human"


class GoldOriginalExpert(Config):
    """Historical human origin, confirmed from a cited original record."""

    expert: GoldHuman | None = None
    annotated_at: AwareDatetime | None = None
    basis: str = Field(min_length=1, pattern=r"\S")
    unknown_reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def known_or_explained(self) -> GoldOriginalExpert:
        if (
            self.expert is None or self.annotated_at is None
        ) and not self.unknown_reason:
            raise GoldFormatError("未知的原始专家或时间须说明缺失原因")
        return self


class GoldPreparationAuthor(Config):
    """Who prepared this particular draft or revision, and why."""

    actor: GoldActor
    prepared_at: AwareDatetime
    action: Literal["initial_draft", "migration", "revision"]
    description: str = Field(min_length=1, pattern=r"\S")


class GoldMaterialCorrespondence(Config):
    """Expert-declared fixed PDF scope; hashing does not prove scientific correctness."""

    status: Literal["pending", "checked"] = "pending"
    pdf_file_ids: list[str] = Field(default_factory=list)
    checked_by: GoldHuman | None = None
    checked_at: AwareDatetime | None = None
    basis: str = ""
    differences: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def checked_requires_evidence(self) -> GoldMaterialCorrespondence:
        if self.status == "checked" and (
            not self.pdf_file_ids
            or self.checked_by is None
            or self.checked_at is None
            or not self.basis.strip()
        ):
            raise GoldFormatError(
                "已核对的材料对应必须包含固定材料、核对专家、时间及依据"
            )
        if len(self.pdf_file_ids) != len(set(self.pdf_file_ids)):
            raise GoldFormatError("材料对应中 PDF 引用重复")
        return self


class GoldPdfFile(Config):
    """One fixed PDF material; an unverified hash stays pending rather than fabricated."""

    file_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    sha256: str | None = Field(default=None, pattern=_SHA256_RE)

    @model_validator(mode="after")
    def relative_name(self) -> GoldPdfFile:
        parts = self.name.replace("\\", "/").split("/")
        if self.name.startswith(("/", "~")) or any(
            part in {"", ".."} for part in parts
        ):
            raise GoldFormatError(f"材料文件名越界或不是相对路径：{self.name}")
        return self


class GoldEvidence(Config):
    """Location where an expert saw the content in the fixed PDF.

    Preserve location descriptions (page/table/section/object row) verbatim. page is the
    one-based file page ordinal, distinct from printed page numbers; fill only when
    explicitly known, never inferred from text. quote is optional; an empty string means
    absent.
    """

    file_id: str | None = None
    location: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    quote: str = ""

    @model_validator(mode="after")
    def locator_shape(self) -> GoldEvidence:
        if not self.location.strip():
            raise GoldFormatError("证据位置说明不能是空白")
        return self


class GoldProbability(Config):
    """Explicitly printed coverage probability or quantile; consumers perform conversions
    without writing them back.
    """

    value: str = Field(min_length=1)
    unit: Literal["fraction", "%"]
    evidence: list[GoldEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def probability_scale(self) -> GoldProbability:
        if parse_probability(self.value, self.unit) is None:
            scale = "0–1 fraction" if self.unit == "fraction" else "0–100 percent"
            raise GoldFormatError(
                f"概率取值必须落在声明的比例尺（{scale}）内：{self.value!r}"
            )
        return self


class GoldErrorComponent(Config):
    """One named original error component, without inferred total errors.

    Follow shared ErrorComponent rules: one-sided asymmetric errors are allowed,
    symmetric/asymmetric forms cannot mix, and at least one error value is required.
    """

    name: str = Field(min_length=1)
    error: str | None
    lower_error: str | None
    upper_error: str | None
    evidence: list[GoldEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def component_shape(self) -> GoldErrorComponent:
        for part in ("error", "lower_error", "upper_error"):
            number = getattr(self, part)
            if number is not None and (
                not number.strip() or parse_plain_number(number) is None
            ):
                raise GoldFormatError(
                    f"误差分量 {self.name} 的 {part} 必须是非空普通数字文本"
                )
        filled = {
            part
            for part in ("error", "lower_error", "upper_error")
            if getattr(self, part) is not None
        }
        if "error" in filled and filled - {"error"}:
            raise GoldFormatError(
                f"误差分量 {self.name} 不能同时使用对称误差和非对称误差"
            )
        if not filled:
            raise GoldFormatError(f"误差分量 {self.name} 至少提供一个误差数值")
        return self


class GoldValue(Config):
    """Gold representation of scientific values; absent contents are null and drafts permit
    empty evidence.

    Follow shared MeasuredValue rules: allow one-sided asymmetric errors and
    independently evidenced total/named components together, but never mix
    symmetric/asymmetric forms. Represent empty strings as absent null values.
    """

    value: str | None
    error: str | None
    lower_error: str | None
    upper_error: str | None
    unit: str | None
    limit_kind: Literal[GOLD_LIMIT_KINDS] = "none"  # type: ignore[valid-type]
    range_lower: str | None
    range_upper: str | None
    coordinate_format: (
        Literal[GOLD_COORDINATE_FORMATS] | None  # type: ignore[valid-type]
    ) = None
    condition: str | None
    interval_kind: Literal[GOLD_INTERVAL_KINDS] | None = None  # type: ignore[valid-type]
    coverage: GoldProbability | None = None
    lower_quantile: GoldProbability | None = None
    upper_quantile: GoldProbability | None = None
    error_components: list[GoldErrorComponent] = Field(min_length=0)
    representation: Literal[GOLD_REPRESENTATIONS] | None = None  # type: ignore[valid-type]
    source: Literal["this_paper", "prior_work", "unclear"]
    source_note: str | None
    evidence: list[GoldEvidence] = Field(min_length=0)
    context_evidence: list[GoldEvidence] = Field(min_length=0)

    @model_validator(mode="after")
    def value_shape(self) -> GoldValue:
        for part in (
            "value",
            "error",
            "lower_error",
            "upper_error",
            "range_lower",
            "range_upper",
        ):
            number = getattr(self, part)
            if number is not None and not number.strip():
                raise GoldFormatError(f"{part} 为空时应使用 null，不能是空字符串")
        if self.error is not None and (
            self.lower_error is not None or self.upper_error is not None
        ):
            raise GoldFormatError("对称误差和非对称误差不能同时使用")
        if self.limit_kind == "range":
            if self.value is not None:
                raise GoldFormatError("纯区间不能填写中心值")
            if self.range_lower is None or self.range_upper is None:
                raise GoldFormatError("纯区间必须提供两端端点")
        else:
            if self.value is None:
                raise GoldFormatError("点值和界限必须提供 value")
            if self.range_lower is not None or self.range_upper is not None:
                raise GoldFormatError("非区间形式不能填写区间端点")
        return self

    @model_validator(mode="after")
    def interval_extensions(self) -> GoldValue:
        if self.limit_kind == "range":
            if self.interval_kind is None:
                raise GoldFormatError(
                    "纯区间必须给出区间解释，原文未说明时用 unspecified"
                )
        elif any(
            getattr(self, part) is not None
            for part in (
                "interval_kind",
                "coverage",
                "lower_quantile",
                "upper_quantile",
            )
        ):
            raise GoldFormatError("区间元数据只能用于纯区间")
        names = [component.name for component in self.error_components]
        if len(names) != len(set(names)):
            raise GoldFormatError("误差分量名称重复")
        if self.lower_quantile is not None and self.upper_quantile is not None:

            def fraction(item: GoldProbability) -> float:
                number = parse_plain_number(item.value)
                assert number is not None  # Already passed its own validation.
                return number / (100.0 if item.unit == "%" else 1.0)

            if fraction(self.lower_quantile) >= fraction(self.upper_quantile):
                raise GoldFormatError("下分位点必须低于上分位点")
        return self

    def numeric_evidence_sources(self) -> list[GoldEvidence]:
        sources: list[GoldEvidence] = []
        for node in (
            self,
            *self.error_components,
            *(
                value
                for value in (self.coverage, self.lower_quantile, self.upper_quantile)
                if value is not None
            ),
        ):
            sources.extend(node.evidence)
        return sources

    def canonical_key(self) -> str:
        """Deterministic expression key with sorted evidence sets for duplicate/conflict
        checks.
        """
        data = self.model_dump(mode="json")
        data["evidence"] = sorted(data.get("evidence", []), key=canonical_json)
        data["context_evidence"] = sorted(
            data.get("context_evidence", []), key=canonical_json
        )
        return canonical_json(data)


class GoldQuantityGroup(Config):
    quantity: str = Field(min_length=1)
    values: list[GoldValue] = Field(min_length=1)

    @model_validator(mode="after")
    def check_group(self) -> GoldQuantityGroup:
        if self.quantity not in quantity_paths():
            raise GoldFormatError(f"未知的物理量编码：{self.quantity}")
        coordinate_quantity = self.quantity in COORDINATE_QUANTITY_RANGES
        keys: set[str] = set()
        for item in self.values:
            if coordinate_quantity and item.coordinate_format is None:
                raise GoldFormatError("坐标值必须给出坐标格式")
            if not coordinate_quantity and item.coordinate_format is not None:
                raise GoldFormatError("coordinate_format 只用于 RA 和 Dec")
            if self.quantity.endswith(".ra") and item.coordinate_format == (
                "sexagesimal_dms"
            ):
                raise GoldFormatError("RA 不能使用 sexagesimal_dms")
            if self.quantity.endswith(".dec") and item.coordinate_format == (
                "sexagesimal_hms"
            ):
                raise GoldFormatError("Dec 不能使用 sexagesimal_hms")
            if self.quantity in (
                "bound_assessment.bound_probability",
                "bound_assessment.unbound_probability",
            ):
                unit = str(item.unit or "")
                if unit not in ("", "%"):
                    raise GoldFormatError("概率单位必须为空（分数）或 '%'（百分数）")
                upper = 100.0 if unit == "%" else 1.0
                for part in ("value", "range_lower", "range_upper"):
                    number_text = getattr(item, part)
                    if number_text is None:
                        continue
                    number = parse_plain_number(number_text)
                    if number is not None and not 0.0 <= number <= upper:
                        scale = "0–100 百分数" if unit == "%" else "0–1 分数"
                        raise GoldFormatError(f"概率 {part} 必须落在{scale}内")
            for part in (
                "value",
                "error",
                "lower_error",
                "upper_error",
                "range_lower",
                "range_upper",
            ):
                number_text = getattr(item, part)
                if number_text is None:
                    continue
                if part == "value" and coordinate_quantity:
                    degrees = parse_sexagesimal(number_text)
                    if degrees is None:
                        degrees = parse_plain_number(number_text)
                    if degrees is not None:
                        lower, upper, label, upper_inclusive = (
                            COORDINATE_QUANTITY_RANGES[self.quantity]
                        )
                        above_upper = (
                            degrees > upper if upper_inclusive else degrees >= upper
                        )
                        if degrees < lower or above_upper:
                            raise GoldFormatError(
                                f"{self.quantity} 超出坐标范围 {label}：{number_text}"
                            )
                        continue
                elif parse_plain_number(number_text) is not None:
                    continue
                raise GoldFormatError(
                    f"{part} 必须是允许的原文数值文本（坐标可用六十进制）："
                    f"{number_text!r}"
                )
            key = item.canonical_key()
            if key in keys:
                raise GoldFormatError(f"物理量 {self.quantity} 存在重复数值")
            keys.add(key)
        if any(
            value.representation == "distance_modulus"
            and self.quantity != "observed_phase_space.distance"
            for value in self.values
        ):
            raise GoldFormatError("distance_modulus 表示只用于距离")
        return self


class GoldConflict(Config):
    """Unresolved competing evidence; complete alternative value structures are optional,
    with missing contents reported for completion.
    """

    conflict_id: str = Field(min_length=1)
    quantity: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence: list[GoldEvidence] = Field(min_length=2)
    alternatives: list[GoldValue] = Field(min_length=0)

    @model_validator(mode="after")
    def conflict_shape(self) -> GoldConflict:
        if self.quantity not in quantity_paths():
            raise GoldFormatError(f"未知的物理量编码：{self.quantity}")
        if not self.reason.strip():
            raise GoldFormatError("冲突原因不能是空白")
        locators = {
            json.dumps(item.model_dump(mode="json"), sort_keys=True)
            for item in self.evidence
        }
        if len(locators) < 2:
            raise GoldFormatError("冲突必须有两个不同的证据位置")
        if self.alternatives:
            if len(self.alternatives) < 2:
                raise GoldFormatError("冲突备选数值至少两方")
            expressions = [item.canonical_key() for item in self.alternatives]
            if any(
                expressions[i] == expressions[j]
                for i in range(len(expressions))
                for j in range(i + 1, len(expressions))
            ):
                raise GoldFormatError("冲突备选必须有不同的完整数值表达")
        return self


class GoldExclusion(Config):
    """Paper-level exclusions; missing independent object IDs in legacy structures remain
    pending correspondence items.
    """

    exclusion_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence: list[GoldEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def reason_required(self) -> GoldExclusion:
        if not self.reason.strip():
            raise GoldFormatError("排除原因不能是空白")
        return self


class GoldIdentifier(Config):
    value: str = Field(min_length=1)
    evidence: list[GoldEvidence] = Field(min_length=0)


class GoldBoundness(Config):
    """Overall author state: unresolved new vocabulary state is null; preserve legacy state
    verbatim for checking.
    """

    status: str | None = None
    legacy_status: str | None = None
    evidence: list[GoldEvidence] = Field(min_length=0)

    @model_validator(mode="after")
    def known_statuses(self) -> GoldBoundness:
        states = author_state_ids()
        if self.status is not None and self.status not in states:
            raise GoldFormatError(
                f"未知的作者状态：{self.status!r}（当前规则词表 {list(states)}）"
            )
        if self.legacy_status is not None and self.legacy_status not in (
            LEGACY_AUTHOR_STATUSES
        ):
            raise GoldFormatError(
                f"未知的旧作者状态：{self.legacy_status!r}"
                f"（旧词表 {list(LEGACY_AUTHOR_STATUSES)}）"
            )
        return self


class GoldContribution(Config):
    contribution_id: str = Field(min_length=1)
    legacy_index: int | None = Field(default=None, ge=0)
    identifiers: list[GoldIdentifier] = Field(min_length=1)
    contribution_type: Literal["candidates_found", "follow_up"]
    contribution_summary: str = Field(min_length=1)
    contribution_evidence: list[GoldEvidence] = Field(min_length=0)
    paper_boundness: GoldBoundness
    quantities: list[GoldQuantityGroup] = Field(min_length=0)
    quantity_conflicts: list[GoldConflict] = Field(min_length=0)

    @model_validator(mode="after")
    def check_contribution(self) -> GoldContribution:
        values = [identifier.value for identifier in self.identifiers]
        if len(values) != len(set(values)):
            raise GoldFormatError("同一贡献内名称重复，同一名称只保存一次")
        quantities = [group.quantity for group in self.quantities]
        if len(quantities) != len(set(quantities)):
            raise GoldFormatError("同一种物理量只能占一组")
        for conflict in self.quantity_conflicts:
            deferred = {
                json.dumps(item.model_dump(mode="json"), sort_keys=True)
                for item in conflict.evidence
            }
            if any(
                json.dumps(source.model_dump(mode="json"), sort_keys=True) in deferred
                for group in self.quantities
                if group.quantity == conflict.quantity
                for value in group.values
                for source in value.numeric_evidence_sources()
            ):
                raise GoldFormatError(
                    "冲突中的待决证据不能同时作为已确认数值的直接证据"
                )
        return self


class GoldLegacySource(Config):
    """Migration provenance preserves original legacy annotation metadata verbatim without
    constituting new approval.
    """

    schema_name: str = Field(min_length=1)
    schema_version: int = Field(ge=1)
    file_name: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256_RE)
    arxiv_id: str = Field(min_length=1)
    annotator: str = Field(min_length=1)
    annotated_at: str = Field(min_length=1)
    guideline_version: str = Field(min_length=1)
    annotation_process: dict = Field(default_factory=dict)
    canary: str = ""


class GoldProvenance(Config):
    origin: Literal["migration", "new_annotation"]
    legacy: GoldLegacySource | None = None
    note: str = ""
    original_expert: GoldOriginalExpert | None = None
    original_approval_version: int | None = Field(default=None, ge=1)
    prepared_by: GoldPreparationAuthor | None = None

    @model_validator(mode="after")
    def origin_matches_legacy(self) -> GoldProvenance:
        if self.origin == "migration" and self.legacy is None:
            raise GoldFormatError("迁移来源必须保留旧输入信息")
        if self.origin == "new_annotation" and self.legacy is not None:
            raise GoldFormatError("全新标注没有迁移来源")
        if self.origin == "migration" and self.original_approval_version is not None:
            raise GoldFormatError("旧标注原始专家不能由本次批准冒充")
        if self.origin == "new_annotation" and (
            self.original_expert is not None
            or self.original_approval_version not in (None, 1)
        ):
            raise GoldFormatError("全新标注的原始专家由首次人类批准记录确定")
        return self


class GoldPendingItem(Config):
    """Explicit unresolved draft items, forbidden in official answers."""

    item_id: str = Field(min_length=1)
    category: Literal["missing_info", "science_review", "structural_gap"]
    subject: str = Field(min_length=1)
    description: str = Field(min_length=1)


class GoldAnnotation(Config):
    schema_tag: GoldSchemaTag = Field(alias="schema")
    kind: Literal["draft", "answer"]
    document_id: str = Field(min_length=1)
    paper: GoldPaper
    rules: GoldRulesRef
    files: list[GoldPdfFile] = Field(min_length=0)
    status: Literal["contributions_found", "no_contributions"]
    contributions: list[GoldContribution] = Field(min_length=0)
    reviewed_exclusions: list[GoldExclusion] = Field(min_length=0)
    provenance: GoldProvenance
    material_correspondence: GoldMaterialCorrespondence = Field(
        default_factory=GoldMaterialCorrespondence
    )
    pending: list[GoldPendingItem] = Field(default_factory=list)

    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, populate_by_name=True
    )

    @model_validator(mode="after")
    def check_document(self) -> GoldAnnotation:
        if self.status == "no_contributions" and self.contributions:
            raise GoldFormatError("no_contributions 文档不能列出贡献")
        if self.status == "contributions_found" and not self.contributions:
            raise GoldFormatError("contributions_found 文档必须有贡献")
        file_ids = [file.file_id for file in self.files]
        if len(file_ids) != len(set(file_ids)):
            raise GoldFormatError("文档内 file_id 重复")
        contribution_ids = [item.contribution_id for item in self.contributions]
        if len(contribution_ids) != len(set(contribution_ids)):
            raise GoldFormatError("文档内 contribution_id 重复")
        conflict_ids = [
            conflict.conflict_id
            for item in self.contributions
            for conflict in item.quantity_conflicts
        ]
        if len(conflict_ids) != len(set(conflict_ids)):
            raise GoldFormatError("文档内 conflict_id 重复")
        exclusion_ids = [item.exclusion_id for item in self.reviewed_exclusions]
        if len(exclusion_ids) != len(set(exclusion_ids)):
            raise GoldFormatError("文档内 exclusion_id 重复")
        pending_ids = [item.item_id for item in self.pending]
        if len(pending_ids) != len(set(pending_ids)):
            raise GoldFormatError("文档内待办编号重复")
        owner: dict[str, str] = {}
        for contribution in self.contributions:
            for identifier in contribution.identifiers:
                if identifier.value in owner and owner[identifier.value] != (
                    contribution.contribution_id
                ):
                    raise GoldFormatError(f"名称 {identifier.value!r} 出现在多个贡献中")
                owner[identifier.value] = contribution.contribution_id
        referenced = {evidence.file_id for _, evidence in iter_gold_evidence(self)}
        known = set(file_ids)
        if set(self.material_correspondence.pdf_file_ids) - known:
            raise GoldFormatError("材料对应引用了未登记的材料文件（PDF）")
        unknown = sorted(
            ref for ref in referenced if ref is not None and ref not in known
        )
        if unknown:
            raise GoldFormatError(f"证据引用了未登记的材料文件：{unknown}")
        if self.kind == "answer" and self.pending:
            raise GoldFormatError("正式答案不能携带待办条目")
        return self


def parse_gold_annotation(payload: str | dict | Path) -> GoldAnnotation:
    """Parse Gold annotations, explicitly rejecting unknown versions or fields."""
    if isinstance(payload, Path):
        payload = payload.read_text(encoding="utf-8")
    if isinstance(payload, str):
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise GoldFormatError(f"Gold 文档不是有效的 JSON：{exc}") from exc
    else:
        obj = payload
    if not isinstance(obj, dict):
        raise GoldFormatError("Gold 文档必须是 JSON 对象")
    tag = obj.get("schema")
    name = tag.get("name") if isinstance(tag, dict) else None
    version = tag.get("version") if isinstance(tag, dict) else None
    if name != GOLD_SCHEMA_NAME or version != GOLD_SCHEMA_VERSION:
        raise GoldFormatError(
            f"Gold 文档的 schema 必须是 {GOLD_SCHEMA_NAME!r} "
            f"v{GOLD_SCHEMA_VERSION}，实际 {name!r} v{version}"
        )
    try:
        return GoldAnnotation.model_validate(assign_technical_ids(obj))
    except GoldFormatError:
        raise
    except ValueError as exc:
        raise GoldFormatError(f"Gold 文档结构不满足格式定义：{exc}") from exc


def read_gold_annotation(path: str | Path) -> GoldAnnotation:
    return parse_gold_annotation(Path(path).read_text(encoding="utf-8"))


def gold_annotation_to_json(annotation: GoldAnnotation) -> str:
    """Serialize deterministically using the same representation to freeze contents during
    preparation and save.
    """
    return (
        json.dumps(
            annotation.model_dump(mode="json", by_alias=True),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def iter_gold_evidence(
    value: object, path: str = ""
) -> Iterator[tuple[str, GoldEvidence]]:
    """Iterate all typed PDF evidence through the shared reference/material-check entry
    point.
    """
    if isinstance(value, GoldEvidence):
        yield path, value
    elif isinstance(value, Config):
        for name in type(value).model_fields:
            yield from iter_gold_evidence(
                getattr(value, name), f"{path}/{name}".lstrip("/")
            )
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from iter_gold_evidence(item, f"{path}/{index}")


def assign_technical_ids(payload: dict) -> dict:
    """Assign missing technical IDs only; preserve all existing IDs and science."""
    import copy

    result = copy.deepcopy(payload)

    def fill(items, field, prefix):
        if not isinstance(items, list):
            return
        used = {
            item.get(field)
            for item in items
            if isinstance(item, dict) and isinstance(item.get(field), str)
        }
        counter = 1
        for item in items:
            if not isinstance(item, dict) or item.get(field) is not None:
                continue
            while f"{prefix}{counter}" in used:
                counter += 1
            item[field] = f"{prefix}{counter}"
            used.add(item[field])

    contributions = result.get("contributions", [])
    fill(contributions, "contribution_id", "c")
    conflicts = (
        [
            c
            for item in contributions
            if isinstance(item, dict)
            and isinstance(item.get("quantity_conflicts", []), list)
            for c in item.get("quantity_conflicts", [])
        ]
        if isinstance(contributions, list)
        else []
    )
    fill(conflicts, "conflict_id", "k")
    fill(result.get("reviewed_exclusions", []), "exclusion_id", "x")
    fill(result.get("pending", []), "item_id", "p")
    return result
