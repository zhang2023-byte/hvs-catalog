"""Read-only adapter for legacy contribution Gold benchmark.hvs_contribution_annotation v4.

Parse/validate without modifying input or silently discarding fields through new models.
Reproduce legacy StrictModel rejection of unknown fields and v4
value/state/coordinate/conflict rules. Exact-value duplicate checks replace internal
cross-object normalization and catch damaged inputs at read time. Explicitly reject
v1-v3 vocabulary/field differences without silent upgrades.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..records.rules import quantity_paths
from .errors import LegacyFormatError
from .numbers import (
    COORDINATE_QUANTITY_RANGES,
    normalize_number_text,
    parse_plain_number,
    parse_probability,
    parse_sexagesimal,
)

LEGACY_SCHEMA_NAME = "benchmark.hvs_contribution_annotation"
LEGACY_SCHEMA_VERSION = 4

#: Legacy overall-author vocabulary; current rules removed not_assessed.
LEGACY_AUTHOR_STATUSES = (
    "unbound",
    "possibly_unbound",
    "bound",
    "no_overall_conclusion",
    "not_assessed",
)

LEGACY_CONTRIBUTION_TYPES = ("candidates_found", "follow_up")
LEGACY_LIMIT_KINDS = ("none", "lower_limit", "upper_limit", "range")
LEGACY_INTERVAL_KINDS = (
    "ordinary_range",
    "prior_support",
    "confidence",
    "credible",
    "statistical_interval",
)
LEGACY_REPRESENTATIONS = ("linear", "log10", "distance_modulus")
LEGACY_COORDINATE_FORMATS = (
    "decimal_degrees",
    "sexagesimal_hms",
    "sexagesimal_dms",
    "sexagesimal_colon",
)
LEGACY_MIGRATION_PROTOCOL = "contribution_migration_ai_assisted_v1"

_ANNOTATOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CANARY_PREFIX = "stella-contribution-gold-canary-v0.1-"


class LegacyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class LegacyEvidence(LegacyModel):
    location: str
    quote: str = ""

    @model_validator(mode="after")
    def location_required(self) -> LegacyEvidence:
        if not self.location.strip():
            raise ValueError("evidence location is required")
        return self


class LegacyAnnotationProcess(LegacyModel):
    protocol: str
    preannotation_agent: str = ""
    preannotation_model: str = ""
    reconciliation_agent: str = ""
    reconciliation_model: str = ""
    expert_review_scope: Literal["paper_level"] = "paper_level"
    process_note: str = ""

    @model_validator(mode="after")
    def process_is_complete(self) -> LegacyAnnotationProcess:
        if not self.protocol.strip():
            raise ValueError("annotation_process.protocol is required")
        if self.protocol == LEGACY_MIGRATION_PROTOCOL:
            for name in (
                "preannotation_agent",
                "preannotation_model",
                "reconciliation_agent",
                "reconciliation_model",
            ):
                if not getattr(self, name).strip():
                    raise ValueError(
                        f"annotation_process.{name} is required for the "
                        "migration protocol"
                    )
        if self.reconciliation_agent == "PENDING_STAGE_B" or (
            self.reconciliation_model == "PENDING_STAGE_B"
        ):
            raise ValueError("final reconciliation identity cannot be PENDING_STAGE_B")
        return self


class LegacyReviewedExclusion(LegacyModel):
    reason: str
    evidence: list[LegacyEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def reason_required(self) -> LegacyReviewedExclusion:
        if not self.reason.strip():
            raise ValueError("reviewed exclusion reason is required")
        return self


class LegacyIdentifier(LegacyModel):
    value: str
    evidence: list[LegacyEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def value_required(self) -> LegacyIdentifier:
        if not self.value.strip():
            raise ValueError("identifier value is required")
        return self


class LegacyPaperBoundness(LegacyModel):
    status: Literal[LEGACY_AUTHOR_STATUSES]  # type: ignore[valid-type]
    evidence: list[LegacyEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def assessed_statuses_need_evidence(self) -> LegacyPaperBoundness:
        if self.status != "not_assessed" and not self.evidence:
            raise ValueError(
                "paper_boundness evidence is required unless status is not_assessed"
            )
        return self


class LegacyIntervalProbability(LegacyModel):
    value: str
    unit: Literal["fraction", "%"]
    evidence: list[LegacyEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def probability_scale(self) -> LegacyIntervalProbability:
        if parse_probability(self.value, self.unit) is None:
            scale = "0--1 fraction" if self.unit == "fraction" else "0--100 percent"
            raise ValueError(
                f"coverage and quantile value must use its stated scale "
                f"({scale}): {self.value!r}"
            )
        return self


class LegacyErrorComponent(LegacyModel):
    name: str
    error: str = ""
    lower_error: str = ""
    upper_error: str = ""
    evidence: list[LegacyEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def component_shape(self) -> LegacyErrorComponent:
        if not self.name.strip():
            raise ValueError("error component name is required")
        populated = {
            part
            for part in ("error", "lower_error", "upper_error")
            if getattr(self, part).strip()
        }
        if populated not in ({"error"}, {"lower_error", "upper_error"}):
            raise ValueError(
                "error component needs symmetric error or both asymmetric errors"
            )
        for part in populated:
            if parse_plain_number(getattr(self, part)) is None:
                raise ValueError(
                    f"error component {part} must be a plain number: "
                    f"{getattr(self, part)!r}"
                )
        return self


def _check_probability_representation(
    quantity: str, *, unit: str, parts: dict[str, str]
) -> None:
    if quantity not in (
        "bound_assessment.bound_probability",
        "bound_assessment.unbound_probability",
    ):
        return
    if unit.strip() not in ("", "%"):
        raise ValueError(
            "probability unit must be empty for a fraction or '%' for a percent"
        )
    upper = 100.0 if unit.strip() == "%" else 1.0
    for part, text in parts.items():
        if not text.strip():
            continue
        number = parse_plain_number(text)
        if number is not None and not 0.0 <= number <= upper:
            scale = "0--100 percent" if upper == 100.0 else "0--1 fraction"
            raise ValueError(f"probability {part} must be a {scale}")


class LegacyReportedValue(LegacyModel):
    value: str = ""
    error: str = ""
    lower_error: str = ""
    upper_error: str = ""
    unit: str = ""
    limit_kind: Literal[LEGACY_LIMIT_KINDS] = "none"  # type: ignore[valid-type]
    range_lower: str = ""
    range_upper: str = ""
    coordinate_format: (
        Literal[LEGACY_COORDINATE_FORMATS] | None  # type: ignore[valid-type]
    ) = None
    condition: str = ""
    source: Literal["this_paper", "prior_work", "unclear"]
    evidence: list[LegacyEvidence] = Field(min_length=1)
    context_evidence: list[LegacyEvidence] = Field(default_factory=list)
    source_note: str = ""
    interval_kind: (
        Literal[LEGACY_INTERVAL_KINDS] | None  # type: ignore[valid-type]
    ) = None
    coverage: LegacyIntervalProbability | None = None
    lower_quantile: LegacyIntervalProbability | None = None
    upper_quantile: LegacyIntervalProbability | None = None
    error_components: list[LegacyErrorComponent] = Field(default_factory=list)
    representation: (
        Literal[LEGACY_REPRESENTATIONS] | None  # type: ignore[valid-type]
    ) = None

    @model_validator(mode="after")
    def value_and_limit_shape(self) -> LegacyReportedValue:
        if self.error.strip() and (
            self.lower_error.strip() or self.upper_error.strip()
        ):
            raise ValueError("symmetric and asymmetric uncertainties cannot mix")
        if bool(self.lower_error.strip()) != bool(self.upper_error.strip()):
            raise ValueError(
                "asymmetric uncertainty requires both lower and upper errors"
            )
        if self.limit_kind == "range":
            if self.value.strip():
                raise ValueError("range values keep value empty")
            if not (self.range_lower.strip() and self.range_upper.strip()):
                raise ValueError("range values need both range bounds")
        else:
            if not self.value.strip():
                raise ValueError("non-range values need a value")
            if self.range_lower.strip() or self.range_upper.strip():
                raise ValueError("range bounds require limit_kind 'range'")
        return self

    @model_validator(mode="after")
    def v4_extensions(self) -> LegacyReportedValue:
        simple_errors = any(
            getattr(self, part).strip()
            for part in ("error", "lower_error", "upper_error")
        )
        if self.limit_kind == "range":
            if self.interval_kind is None:
                raise ValueError("range values require interval_kind")
            if simple_errors or self.error_components:
                raise ValueError(
                    "standalone intervals cannot carry central-value errors"
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
            raise ValueError("interval metadata requires limit_kind range")
        if simple_errors and self.error_components:
            raise ValueError("simple errors and named error_components cannot coexist")
        names = [component.name.strip() for component in self.error_components]
        if len(names) != len(set(names)):
            raise ValueError("error component names must be unique")
        if self.lower_quantile is not None and self.upper_quantile is not None:

            def fraction(item: LegacyIntervalProbability) -> float:
                number = parse_plain_number(item.value)
                assert number is not None  # Already passed its own validation.
                return number / (100.0 if item.unit == "%" else 1.0)

            if fraction(self.lower_quantile) >= fraction(self.upper_quantile):
                raise ValueError("lower quantile must be below upper quantile")
        return self

    def numeric_evidence_sources(self) -> list[LegacyEvidence]:
        """Direct numeric evidence, including nested coverage probabilities, quantiles, and
        error components.
        """
        sources: list[LegacyEvidence] = []
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


class LegacyQuantityGroup(LegacyModel):
    quantity: str
    values: list[LegacyReportedValue] = Field(min_length=1)

    @model_validator(mode="after")
    def check_group(self) -> LegacyQuantityGroup:
        known = quantity_paths()
        if self.quantity not in known:
            raise ValueError(f"unknown structured quantity: {self.quantity!r}")
        seen: set[str] = set()
        for item in self.values:
            coordinate_quantity = self.quantity in COORDINATE_QUANTITY_RANGES
            if coordinate_quantity and item.coordinate_format is None:
                raise ValueError("coordinate values require coordinate_format")
            if not coordinate_quantity and item.coordinate_format is not None:
                raise ValueError("coordinate_format is only valid for RA and Dec")
            if self.quantity.endswith(".ra") and item.coordinate_format == (
                "sexagesimal_dms"
            ):
                raise ValueError("RA cannot use sexagesimal_dms")
            if self.quantity.endswith(".dec") and item.coordinate_format == (
                "sexagesimal_hms"
            ):
                raise ValueError("Dec cannot use sexagesimal_hms")
            _check_probability_representation(
                self.quantity,
                unit=item.unit,
                parts={
                    part: getattr(item, part)
                    for part in (
                        "value",
                        "error",
                        "lower_error",
                        "upper_error",
                        "range_lower",
                        "range_upper",
                    )
                },
            )
            for part in (
                "value",
                "error",
                "lower_error",
                "upper_error",
                "range_lower",
                "range_upper",
            ):
                text = getattr(item, part)
                if not text.strip():
                    continue
                if part == "value" and coordinate_quantity:
                    degrees = parse_sexagesimal(text)
                    if degrees is None:
                        degrees = parse_plain_number(text)
                    if degrees is None:
                        raise ValueError(
                            f"{part} must be a plain number or sexagesimal "
                            f"coordinate, got {text!r}"
                        )
                    lower, upper, label, upper_inclusive = COORDINATE_QUANTITY_RANGES[
                        self.quantity
                    ]
                    above_upper = (
                        degrees > upper if upper_inclusive else degrees >= upper
                    )
                    if degrees < lower or above_upper:
                        raise ValueError(
                            f"{self.quantity} out of range {label}: {text}"
                        )
                elif parse_plain_number(text) is None:
                    raise ValueError(
                        f"{part} must be a plain number, got {text!r} "
                        "(operators go to limit_kind, units to unit, "
                        "qualifiers to source_note)"
                    )
            key = json.dumps(
                item.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
            )
            if key in seen:
                raise ValueError(
                    f"duplicate reported value for quantity {self.quantity}"
                )
            seen.add(key)
        if any(
            value.representation == "distance_modulus"
            and self.quantity != "observed_phase_space.distance"
            for value in self.values
        ):
            raise ValueError(
                "distance_modulus representation is only valid for distance"
            )
        return self


class LegacyQuantityConflict(LegacyModel):
    quantity: str
    reason: str = Field(min_length=1)
    evidence: list[LegacyEvidence] = Field(min_length=2)

    @model_validator(mode="after")
    def conflict_shape(self) -> LegacyQuantityConflict:
        if not self.reason.strip():
            raise ValueError("quantity conflict reason is required")
        if self.quantity not in quantity_paths():
            raise ValueError(f"unknown structured quantity: {self.quantity!r}")
        locators = {
            json.dumps(item.model_dump(mode="json"), sort_keys=True)
            for item in self.evidence
        }
        if len(locators) < 2:
            raise ValueError(
                "quantity conflict requires two distinct evidence locators"
            )
        return self


class LegacyContribution(LegacyModel):
    identifiers: list[LegacyIdentifier] = Field(min_length=1)
    contribution_type: Literal[LEGACY_CONTRIBUTION_TYPES]  # type: ignore[valid-type]
    contribution_summary: str
    contribution_evidence: list[LegacyEvidence] = Field(min_length=1)
    paper_boundness: LegacyPaperBoundness
    quantities: list[LegacyQuantityGroup] = Field(default_factory=list)
    quantity_conflicts: list[LegacyQuantityConflict] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_contribution(self) -> LegacyContribution:
        raw = [identifier.value for identifier in self.identifiers]
        if len(raw) != len(set(raw)):
            raise ValueError("identical raw identifiers must merge their evidence")
        if not self.contribution_summary.strip():
            raise ValueError("contribution_summary is required")
        if (
            self.contribution_type == "candidates_found"
            and self.paper_boundness.status in ("bound", "not_assessed")
        ):
            raise ValueError(
                "candidates_found cannot use paper_boundness bound or not_assessed"
            )
        quantities = [group.quantity for group in self.quantities]
        if len(quantities) != len(set(quantities)):
            raise ValueError("each structured quantity occurs at most once")
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
                raise ValueError(
                    "a deferred conflict printing cannot also be a numeric result"
                )
        return self


class LegacySchemaTag(LegacyModel):
    name: str
    version: int

    @model_validator(mode="after")
    def known_schema(self) -> LegacySchemaTag:
        if self.name != LEGACY_SCHEMA_NAME:
            raise LegacyFormatError(
                f"旧文档的 schema.name 不是 {LEGACY_SCHEMA_NAME!r}：{self.name!r}"
            )
        if self.version != LEGACY_SCHEMA_VERSION:
            raise LegacyFormatError(
                f"旧文档版本 v{self.version} 不在受支持范围（首版只支持 "
                f"{LEGACY_SCHEMA_NAME} v{LEGACY_SCHEMA_VERSION}）；更早版本"
                "涉及词表与字段差异，不静默升级，先报告并补充对应方案"
            )
        return self


class LegacyAnnotation(LegacyModel):
    schema_tag: LegacySchemaTag = Field(alias="schema")
    arxiv_id: str
    annotator: str
    annotated_at: str
    guideline_version: str
    evidence_basis: Literal["pdf"] = "pdf"
    annotation_process: LegacyAnnotationProcess
    canary: str = ""
    status: Literal["contributions_found", "no_contributions"]
    contributions: list[LegacyContribution] = Field(default_factory=list)
    reviewed_exclusions: list[LegacyReviewedExclusion] = Field(default_factory=list)

    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, populate_by_name=True
    )

    @model_validator(mode="after")
    def check_document(self) -> LegacyAnnotation:
        for name in ("arxiv_id", "annotator", "annotated_at", "guideline_version"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if not _ANNOTATOR_RE.fullmatch(self.annotator):
            raise ValueError(f"invalid annotator: {self.annotator!r}")
        if self.status == "no_contributions" and self.contributions:
            raise ValueError("no_contributions documents must not list contributions")
        if self.status == "contributions_found" and not self.contributions:
            raise ValueError("contributions_found documents need contributions")
        owner: dict[str, int] = {}
        for index, contribution in enumerate(self.contributions):
            for identifier in contribution.identifiers:
                if identifier.value in owner and owner[identifier.value] != index:
                    raise ValueError(
                        "identifier values must be unique across "
                        f"contributions: {identifier.value!r}"
                    )
                owner[identifier.value] = index
        return self


def parse_legacy_annotation(payload: str | dict | Path) -> LegacyAnnotation:
    """Parse and validate legacy contribution Gold without modifying input."""
    if isinstance(payload, Path):
        payload = payload.read_text(encoding="utf-8")
    if isinstance(payload, str):
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise LegacyFormatError(f"旧文档不是有效的 JSON：{exc}") from exc
    else:
        obj = payload
    if not isinstance(obj, dict):
        raise LegacyFormatError("旧文档必须是 JSON 对象")
    tag = obj.get("schema")
    name = tag.get("name") if isinstance(tag, dict) else None
    version = tag.get("version") if isinstance(tag, dict) else None
    if name != LEGACY_SCHEMA_NAME:
        raise LegacyFormatError(
            f"旧文档的 schema.name 不是 {LEGACY_SCHEMA_NAME!r}：{name!r}"
        )
    if version != LEGACY_SCHEMA_VERSION:
        raise LegacyFormatError(
            f"旧文档版本 v{version} 不在受支持范围（首版只支持 "
            f"{LEGACY_SCHEMA_NAME} v{LEGACY_SCHEMA_VERSION}）；更早版本涉及"
            "词表与字段差异，不静默升级，先报告并补充对应方案"
        )
    try:
        return LegacyAnnotation.model_validate(obj)
    except LegacyFormatError:
        raise
    except ValueError as exc:
        raise LegacyFormatError(f"旧文档结构不满足 v4 定义：{exc}") from exc


def read_legacy_annotation(path: str | Path) -> LegacyAnnotation:
    """Read-only legacy file parsing leaves contents and timestamps unchanged."""
    return parse_legacy_annotation(Path(path).read_text(encoding="utf-8"))


def legacy_document_sha256(payload: str | dict) -> str:
    """SHA-256 of document bytes, encoding strings unchanged."""
    data = (
        payload.encode("utf-8")
        if isinstance(payload, str)
        else json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    )
    return hashlib.sha256(data).hexdigest()


def legacy_canary(document: dict) -> str:
    """Reproduce the legacy canary from canonical contents excluding the canary field."""
    payload = dict(document)
    payload.pop("canary", None)
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"{_CANARY_PREFIX}{digest}"


def verify_legacy_canary(annotation: LegacyAnnotation, *, document: dict) -> bool:
    """Check whether the legacy document canary matches its contents.

    Return true when no canary exists because verification is unavailable, not
    erroneous. Canaries mark leakage audits; matching never constitutes scientific
    approval.
    """
    recorded = str(document.get("canary") or "")
    if not recorded:
        return True
    return recorded == legacy_canary(document)


def legacy_plain_number_normalized(text: str) -> str | None:
    """Expose normalized numeric text for reports without rewriting source text."""
    normalized = normalize_number_text(text)
    return normalized or None
