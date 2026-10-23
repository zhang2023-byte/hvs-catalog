"""Standalone scientific results, independent of extraction and run records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .rules import load_rules

NonEmptyText = Annotated[str, Field(min_length=1)]
QuantityId = Literal[tuple(quantity.id for quantity in load_rules().quantities)]


class _ScientificContent(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        validate_assignment=True,
    )


class LiteratureMetadata(_ScientificContent):
    """Bibliographic identity of the fixed paper version."""

    arxiv_id: str = Field(
        pattern=r"^(?:[0-9]{4}\.[0-9]{4,5}|[a-zA-Z-]+(?:\.[A-Z]{2})?/[0-9]{7})$",
        description="Bare arXiv identifier, without a version suffix.",
    )
    version: str = Field(
        pattern=r"^v[1-9][0-9]*$",
        description="arXiv paper version, such as v1; not a format version.",
    )
    title: NonEmptyText = Field(description="Paper title.")
    authors: list[NonEmptyText] = Field(description="Paper authors in reported order.")


class TextEvidence(_ScientificContent):
    """An exact excerpt from one supplied text block."""

    location: str = Field(
        pattern=r"^T:p[1-9][0-9]*-b[0-9]+$",
        description="Complete text location, such as T:p3-b5.",
    )
    quote: NonEmptyText = Field(description="Unchanged excerpt from this text block.")


class ImageEvidence(_ScientificContent):
    """A supplied image location without a text transcription."""

    location: str = Field(
        pattern=r"^I:p[1-9][0-9]*-b[0-9]+$",
        description="Complete image location, such as I:p5-b6.",
    )


LiteratureEvidence = TextEvidence | ImageEvidence
EvidenceList = Annotated[list[LiteratureEvidence], Field(min_length=1)]


class LiteratureIdentifier(_ScientificContent):
    """One reported identifier and its supporting evidence."""

    value: NonEmptyText = Field(description="Star identifier.")
    evidence: EvidenceList = Field(description="Evidence for this identifier.")


class LiteratureContributionType(_ScientificContent):
    """The paper's contribution to this star."""

    value: Literal["candidates_found", "follow_up"] = Field(
        description="Contribution category. Scientific inclusion rules are separate."
    )
    evidence: EvidenceList = Field(description="Evidence for this contribution type.")


def _value_schema(schema: dict[str, Any]) -> None:
    """Expose omitted fields and numerical forms identically to Python validation."""
    for prop in schema["properties"].values():
        if prop.get("default", ...) is None:
            prop.pop("default")
            alternatives = prop.pop("anyOf")
            prop.update(
                next(item for item in alternatives if item.get("type") != "null")
            )
    schema.update(
        {
            "oneOf": [
                {
                    "required": ["value"],
                    "not": {
                        "anyOf": [
                            {"required": ["lower_bound"]},
                            {"required": ["upper_bound"]},
                        ]
                    },
                },
                {
                    "anyOf": [
                        {"required": ["lower_bound"]},
                        {"required": ["upper_bound"]},
                    ],
                    "not": {
                        "anyOf": [
                            {"required": [name]}
                            for name in ("value", "error", "lower_error", "upper_error")
                        ]
                    },
                },
            ],
            "not": {
                "required": ["error"],
                "anyOf": [
                    {"required": ["lower_error"]},
                    {"required": ["upper_error"]},
                ],
            },
            "dependentRequired": {
                "lower_inclusive": ["lower_bound"],
                "upper_inclusive": ["upper_bound"],
            },
        }
    )


_OptionalText = Annotated[
    NonEmptyText | None, Field(exclude_if=lambda value: value is None)
]
_OptionalBool = Annotated[bool | None, Field(exclude_if=lambda value: value is None)]


class LiteratureValue(_ScientificContent):
    """One point value, one-sided limit, or pure interval with local evidence."""

    model_config = ConfigDict(json_schema_extra=_value_schema)

    value: _OptionalText = Field(default=None, description="Reported point value.")
    error: _OptionalText = Field(default=None, description="Reported symmetric error.")
    lower_error: _OptionalText = Field(
        default=None, description="Reported lower error, preserving its original sign."
    )
    upper_error: _OptionalText = Field(
        default=None, description="Reported upper error, preserving its original sign."
    )
    lower_bound: _OptionalText = Field(
        default=None, description="Reported lower limit or interval endpoint."
    )
    upper_bound: _OptionalText = Field(
        default=None, description="Reported upper limit or interval endpoint."
    )
    lower_inclusive: _OptionalBool = Field(
        default=None, description="Whether the reported lower bound is included."
    )
    upper_inclusive: _OptionalBool = Field(
        default=None, description="Whether the reported upper bound is included."
    )
    unit: _OptionalText = Field(default=None, description="Unit, when applicable.")
    condition: _OptionalText = Field(
        default=None,
        description="Relevant method, model, reference frame, or statistical meaning.",
    )
    evidence: EvidenceList = Field(
        description="Evidence for this value and its context."
    )

    @field_validator("*", mode="before")
    @classmethod
    def _reject_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("Omit unreported fields instead of using null.")
        return value

    @model_validator(mode="after")
    def _check_form(self) -> Self:
        has_point = self.value is not None
        has_bound = self.lower_bound is not None or self.upper_bound is not None
        has_asymmetric_error = (
            self.lower_error is not None or self.upper_error is not None
        )
        if has_point == has_bound:
            raise ValueError("Provide either a point value or bounds, not both.")
        if has_bound and (self.error is not None or has_asymmetric_error):
            raise ValueError("Bounds cannot have error fields.")
        if self.error is not None and has_asymmetric_error:
            raise ValueError("Symmetric and asymmetric errors cannot be combined.")
        if self.lower_inclusive is not None and self.lower_bound is None:
            raise ValueError("lower_inclusive requires lower_bound.")
        if self.upper_inclusive is not None and self.upper_bound is None:
            raise ValueError("upper_inclusive requires upper_bound.")
        return self


class LiteratureQuantity(_ScientificContent):
    """Reported values grouped by one existing physical quantity identifier."""

    quantity: QuantityId = Field(
        description="Short quantity identifier from the existing science definitions."
    )
    values: list[LiteratureValue] = Field(
        min_length=1, description="Reported values, each with its own evidence."
    )


class LiteratureStar(_ScientificContent):
    """This paper's contribution to one star; cross-paper association is separate."""

    identifiers: list[LiteratureIdentifier] = Field(
        min_length=1, description="Identifiers with individual evidence."
    )
    contribution_type: LiteratureContributionType
    quantities: list[LiteratureQuantity] = Field(
        description="Retained quantities; may be empty if no values are retained."
    )


class LiteratureContribution(_ScientificContent):
    """The complete literature_contribution.json scientific document."""

    metadata: LiteratureMetadata
    stars: list[LiteratureStar] = Field(
        description="Contributions to individual stars; may be empty."
    )
    notes: list[NonEmptyText] = Field(
        description=(
            "Brief notes about relevant exclusions, suspected errors, ambiguities, "
            "and handling decisions with their basis. Put the roster note first. "
            "Follow it with quantity notes in saved roster order. Prefix each "
            "quantity note with that star's first identifier and ': '. "
            "Omit empty notes. Use an empty array when no note is necessary. "
            "Notes do not replace field evidence."
        )
    )


def parse_literature_contribution(
    payload: str | dict[str, Any],
) -> LiteratureContribution:
    """Validate document structure without interpreting scientific contents."""
    if isinstance(payload, str):
        return LiteratureContribution.model_validate_json(payload)
    return LiteratureContribution.model_validate(payload)


def literature_contribution_to_json(document: LiteratureContribution) -> str:
    """Validate and serialize, retaining original strings and omitting absent fields."""
    validated = LiteratureContribution.model_validate(
        document.model_dump(mode="python")
    )
    return (
        json.dumps(validated.model_dump(mode="json"), ensure_ascii=False, indent=2)
        + "\n"
    )


def save_literature_contribution(
    path: str | Path, document: LiteratureContribution
) -> None:
    """Write validated scientific content as UTF-8 JSON; create parent directories."""
    content = literature_contribution_to_json(document)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def load_literature_contribution(path: str | Path) -> LiteratureContribution:
    """Read and validate a standalone scientific document."""
    return parse_literature_contribution(Path(path).read_text(encoding="utf-8"))
