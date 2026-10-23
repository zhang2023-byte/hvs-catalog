"""Scientific inputs for direct annotation scoring."""

from typing import Literal

from pydantic import Field, model_validator

from ...records.common import Config
from ...records.literature_contribution import LiteratureContribution, QuantityId
from ...records.pdf_contribution import GoldAnnotation
from ...records.pdf_contribution import GoldRules as Fingerprint
from ...records.rules import current_rules_ref, load_rules
from .numeric import scheme_reference

GoldReference = GoldAnnotation


class MaterialFile(Config):
    role: Literal["pdf", "supplement"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MaterialBinding(Config):
    files: list[MaterialFile] = Field(min_length=1)
    material_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _files(self):
        keys = [(f.role, f.sha256) for f in self.files]
        if len(keys) != len(set(keys)) or not any(f.role == "pdf" for f in self.files):
            raise ValueError("Materials require a PDF and unique file fingerprints.")
        return self

    def identity(self):
        return sorted((f.role, f.sha256) for f in self.files), self.material_sha256


def check_content(content: LiteratureContribution) -> None:
    for star in content.stars:
        quantities = [q.quantity for q in star.quantities]
        if len(quantities) != len(set(quantities)):
            raise ValueError("Each star must have one group per quantity.")


class Prediction(Config):
    """Retained stage content is independent of delivery of a final science file."""

    content: LiteratureContribution
    rules: Fingerprint
    materials: MaterialBinding
    scope: Literal["full", "roster"]
    delivered: bool
    format_valid: bool
    completion: Literal["complete", "partial", "failed", "unfinished"]
    execution_state: str
    errors: list[str] = Field(default_factory=list)
    evidence_checks: dict[str, int] = Field(default_factory=dict)
    provenance: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _status(self):
        check_content(self.content)
        if self.format_valid and not self.delivered:
            raise ValueError("An undelivered result cannot have a valid final format.")
        if (
            self.scope == "full"
            and self.delivered
            and self.completion not in {"complete", "partial"}
        ):
            raise ValueError(
                "A final science file requires a complete or partial result."
            )
        if self.completion == "failed" and self.content.stars:
            raise ValueError("A failed roster cannot retain stars.")
        return self


class ScoringPlan(Config):
    format: Literal["hvs.scoring-plan"] = "hvs.scoring-plan"
    version: Literal[1] = 1
    papers: list[str] = Field(min_length=1)
    scope: Literal["full", "roster"] = "full"
    quantities: list[QuantityId] = Field(
        default_factory=lambda: [q.id for q in load_rules().quantities]
    )
    rules: Fingerprint = Field(
        default_factory=lambda: Fingerprint(**current_rules_ref())
    )
    scheme: Fingerprint = Field(
        default_factory=lambda: Fingerprint(**scheme_reference())
    )
    program: Fingerprint
    provenance: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique(self):
        if len(self.papers) != len(set(self.papers)) or len(self.quantities) != len(
            set(self.quantities)
        ):
            raise ValueError("Planned papers and quantities must be unique.")
        if self.scope == "full" and not self.quantities:
            raise ValueError("Full scoring requires a declared quantity scope.")
        if self.scope == "roster" and self.quantities:
            raise ValueError("Roster scoring must use an empty quantity scope.")
        return self
