"""Readable publications with complete, delivered per-star provenance."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .catalog import (
    AssociationOrigin,
    CatalogContributionType,
    CatalogId,
    CatalogIdentifier,
    CatalogQuantity,
    SimbadCandidate,
)
from .catalog_review import ReviewDecision
from .catalog_simbad import CompactSimbad, SimbadSummary
from .common import ArtifactRef, Config

RecordPointer = Annotated[str, Field(pattern=r"^/stars/(0|[1-9][0-9]*)$")]


class StarSource(Config):
    title: str
    authors: list[str]
    pdf_url: str
    records: list[RecordPointer] = Field(
        min_length=1,
        description="Original object pointers in this exact paper version's science file.",
    )


class IdentifierAssociation(Config):
    method: Literal["identifier"] = "identifier"
    identifier: str
    nodes: list[str] = Field(min_length=2)


class IdentityLink(Config):
    method: Literal["identifier", "coordinates", "paper", "simbad", "review"]
    left: str
    right: str
    accepted: bool = True
    historical: bool = False
    details: dict
    origins: dict[str, dict] = Field(
        default_factory=dict,
        description="Local references only for origins absent from the current paper files.",
    )


class PublishedSimbad(Config):
    status: Literal[
        "not_queried", "matched", "not_found", "ambiguous", "conflict", "failed"
    ]
    method: Literal["identifier", "coordinates", "review"] | None = None
    selected: SimbadCandidate | None = None
    reason: str | None = None
    queries: list[dict] = Field(default_factory=list)
    related_queries: list[dict] = Field(
        default_factory=list,
        description="Queries from other or superseded snapshots, used only by association evidence.",
    )
    matches: list[dict] = Field(
        default_factory=list,
        description="Query matches reference selected or alternatives instead of repeating data.",
    )
    related_matches: list[dict] = Field(default_factory=list)
    alternatives: list[SimbadCandidate] = Field(
        default_factory=list,
        description="Distinct frozen candidate representations required by matching evidence.",
    )
    bindings: list[IdentityLink] = Field(
        default_factory=list,
        description="Adopted coordinate matches with exact source, query, distance and selection.",
    )

    @model_validator(mode="after")
    def selection_state(self):
        if self.status == "matched":
            if self.selected is None or self.method is None:
                raise ValueError(
                    "Matched SIMBAD data requires a method and selected object."
                )
        elif self.selected is not None:
            raise ValueError("Unresolved SIMBAD data cannot select an object.")
        return self


class StellarScience(Config):
    aliases: list[str] = Field(
        default_factory=list,
        exclude_if=lambda v: not v,
        description="Approved display aliases, separate from original paper identifiers.",
    )
    data_version: str
    catalog_id: CatalogId
    display_name: str
    sources: dict[str, StarSource] = Field(min_length=1)
    identifiers: list[CatalogIdentifier] = Field(min_length=1)
    contribution_types: list[CatalogContributionType] = Field(min_length=1)
    quantities: list[CatalogQuantity]
    warnings: list[str]

    @model_validator(mode="after")
    def source_bindings(self):
        entries = [
            *self.identifiers,
            *self.contribution_types,
            *(v for q in self.quantities for v in q.values),
        ]
        if any(v.source not in self.sources for v in entries):
            raise ValueError("Every scientific item requires an embedded source.")
        if len({q.quantity for q in self.quantities}) != len(self.quantities):
            raise ValueError("A quantity must occur in one group.")
        if any(len(s.records) != len(set(s.records)) for s in self.sources.values()):
            raise ValueError("Duplicate source record.")
        return self


class LegacyStellarRecord(StellarScience):
    review_decisions: list[ReviewDecision] = Field(default_factory=list)
    schema_version: Literal[2] = 2
    simbad: PublishedSimbad
    associations: list[IdentifierAssociation | IdentityLink]
    association_origins: dict[str, AssociationOrigin] = Field(default_factory=dict)


class VersionThreeStellarRecord(LegacyStellarRecord):
    schema_version: Literal[3] = 3
    simbad: CompactSimbad


class StellarRecord(StellarScience):
    schema_version: Literal[4, 5] = 5
    simbad: SimbadSummary
    provenance: ArtifactRef = Field(
        description="Complete identity and SIMBAD evidence, relative to the catalog root and bound by SHA-256."
    )


class StellarProvenance(Config):
    review_decisions: list[ReviewDecision] = Field(default_factory=list)
    schema_version: Literal[4, 5] = 5
    data_version: str
    catalog_id: CatalogId
    associations: list[IdentifierAssociation | IdentityLink]
    association_origins: dict[str, AssociationOrigin] = Field(
        default_factory=dict,
        description="Once-only identity evidence for superseded source versions or contents.",
    )
    simbad: PublishedSimbad


class SimbadTrace(Config):
    schema_version: Literal[3] = 3
    data_version: str
    catalog_id: CatalogId
    simbad: PublishedSimbad
    associations: dict[str, IdentityLink] = Field(
        default_factory=dict,
        description="Complete SIMBAD identity links, keyed by their index in the stellar associations list.",
    )
    association_origins: dict[str, AssociationOrigin] = Field(
        default_factory=dict,
        description="Superseded origins needed by the SIMBAD evidence in this file.",
    )


class PaperEntry(Config):
    science: ArtifactRef
    audit: ArtifactRef
    original_audit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pdf_url: str
    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pdf_origin: dict
    incomplete: dict = Field(
        default_factory=dict,
        description="Only unfinished paper/record states and retained scientific diagnostics; absence means complete.",
    )


class PaperIndex(Config):
    schema_version: Literal[2, 3, 4, 5] = 5
    papers: dict[str, PaperEntry]


class StarEntry(Config):
    catalog_id: CatalogId
    display_name: str
    file: ArtifactRef


class StarIndex(Config):
    schema_version: Literal[2, 3, 4, 5] = 5
    next_id: int = Field(ge=1)
    retired_ids: dict[CatalogId, list[CatalogId]]
    stars: list[StarEntry]


class CatalogCounts(Config):
    papers: int = Field(ge=0)
    stars: int = Field(ge=0)
    source_records: int = Field(ge=0)
    values: int = Field(ge=0)


class CatalogRelease(Config):
    schema_version: Literal[2, 3, 4, 5] = 5
    data_version: str
    counts: CatalogCounts
    papers: ArtifactRef
    stars: ArtifactRef
    rules_version: Literal[1, 2, 3, 4, 5, 6]
    software_version: str
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    simbad_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_manifest_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
