"""The shared scientific result with evidence in original PDF files.

Only evidence differs from the extraction result. Numerical fields and their
validation are inherited; PDF annotations do not invent extraction block IDs.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .literature_contribution import (
    LiteratureContribution,
    LiteratureContributionType,
    LiteratureIdentifier,
    LiteratureMetadata,
    LiteratureQuantity,
    LiteratureStar,
    LiteratureValue,
)


class PdfEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)

    page: int = Field(
        ge=1, description="One-based file page ordinal, not printed page number."
    )
    location: str = Field(
        min_length=1,
        description="Visible table, section, object row or other precise location.",
    )

    @field_validator("location")
    @classmethod
    def _nonblank(cls, value):
        if not value.strip():
            raise ValueError("PDF evidence locations must be nonblank.")
        return value


PdfEvidenceList = Annotated[list[PdfEvidence], Field(min_length=1)]


class PdfIdentifier(LiteratureIdentifier):
    evidence: PdfEvidenceList


class PdfContributionType(LiteratureContributionType):
    evidence: PdfEvidenceList


class PdfValue(LiteratureValue):
    evidence: PdfEvidenceList


class PdfQuantity(LiteratureQuantity):
    values: list[PdfValue] = Field(min_length=1)


class PdfStar(LiteratureStar):
    identifiers: list[PdfIdentifier] = Field(min_length=1)
    contribution_type: PdfContributionType
    quantities: list[PdfQuantity]

    @model_validator(mode="after")
    def _unique_quantities(self):
        if len({q.quantity for q in self.quantities}) != len(self.quantities):
            raise ValueError("Each star must have one group per quantity.")
        return self


class PdfContribution(LiteratureContribution):
    stars: list[PdfStar]


def pdf_evidence(document):
    """Yield every evidence path and item, including identifier/type evidence."""
    for i, star in enumerate(document.stars):
        base = f"stars[{i}]"
        for j, identifier in enumerate(star.identifiers):
            for k, evidence in enumerate(identifier.evidence):
                yield f"{base}.identifiers[{j}].evidence[{k}]", evidence
        for k, evidence in enumerate(star.contribution_type.evidence):
            yield f"{base}.contribution_type.evidence[{k}]", evidence
        for j, quantity in enumerate(star.quantities):
            for k, value in enumerate(quantity.values):
                for n, evidence in enumerate(value.evidence):
                    yield f"{base}.quantities[{j}].values[{k}].evidence[{n}]", evidence


class GoldRules(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)
    version: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class GoldMaterial(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)
    path: Literal["paper.pdf"] = "paper.pdf"
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class GoldMetadata(LiteratureMetadata):
    format: Literal["hvs.gold-annotation"] = "hvs.gold-annotation"
    format_version: Literal[1] = 1
    rules: GoldRules
    material: GoldMaterial


class GoldAnnotation(PdfContribution):
    """One complete reference answer bound to one original PDF and science rules.

    Review status is established by the repository workflow, not by a JSON flag.
    """

    metadata: GoldMetadata
