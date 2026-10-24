"""Frozen catalog inputs, self-contained stellar records, and publication manifests."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import ArtifactRef, Config, RecordRef
from .evidence_audit import QuoteMismatch
from .literature_contribution import (
    EvidenceList,
    LiteratureContributionType,
    LiteratureIdentifier,
    LiteratureMetadata,
    LiteratureValue,
    QuantityId,
)

CatalogId = Annotated[str, Field(pattern=r"^hvs-catalog-[0-9]{6,}$")]


class FinalResultSource(Config):
    """The exact historical run behind a final delivery; not a new extraction."""

    root: str = Field(
        min_length=1,
        description="Historical workspace root, relative to the delivery root.",
    )
    run_ref: ArtifactRef = Field(
        description="Original extraction or repair run, relative to the historical root."
    )


class FinalLiteratureResult(Config):
    """The selected paper result and audit, stored in runs/<bare ID>/results/run.json."""

    format: Literal["hvs.final-literature-result"]
    version: Literal[1]
    arxiv_id: str
    arxiv_version: str
    completion: Literal["complete", "partial"]
    result_ref: ArtifactRef = Field(
        description="Final scientific file, relative to the delivery root."
    )
    evidence_audit_ref: ArtifactRef = Field(
        description="Same-generation audit, relative to the delivery root."
    )
    source: FinalResultSource


class PaperSelection(Config):
    """Select an exact extraction or final delivery; paths are relative to its declared root."""

    workspace: str = Field(min_length=1)
    run_ref: ArtifactRef
    archive_ref: ArtifactRef | None = None
    result_ref: ArtifactRef | None = None
    evidence_audit_ref: ArtifactRef | None = None


class ExplicitAssociation(Config):
    """A correspondence explicitly supported by a selected paper."""

    left: str
    right: str
    source: str
    evidence: EvidenceList


class CatalogSelection(Config):
    schema_version: Literal[1] = 1
    data_version: str = Field(min_length=1)
    papers: list[PaperSelection]
    simbad_refresh: list[CatalogId] = Field(
        default_factory=list,
        description="Existing catalog IDs whose external snapshots may be explicitly refreshed.",
    )
    explicit_associations: list[ExplicitAssociation] = Field(default_factory=list)
    identity_assignments: dict[str, CatalogId] = Field(
        default_factory=dict,
        description="Explicit source-node to catalog ID assignments for identity revisions.",
    )
    retired_ids: dict[CatalogId, list[CatalogId]] = Field(
        default_factory=dict,
        description="Explicit old to successor ID mappings; retired IDs are never reused.",
    )


class SourceObject(Config):
    """A retained source object and its extraction completeness and diagnostics."""

    result_index: int = Field(ge=0)
    source_index: int = Field(ge=0)
    status: Literal["complete", "partial", "failed"]
    snapshot_ref: RecordRef | None
    repair_stage_id: str | None = None
    issues: list[dict]
    rejected: dict[str, list[dict]]
    source_paths: list[str]
    evidence_audit: list[QuoteMismatch]


class CatalogSource(LiteratureMetadata):
    paper_notes: list[str]
    result_status: Literal["complete", "partial"]
    roster_status: Literal["complete", "partial", "failed"]
    roster_issues: list[dict]
    roster_rejected: dict[str, list[dict]]
    errors: list[dict]
    result_sha256: str
    run_sha256: str
    input_sha256: str
    pdf_sha256: str
    pdf_url: str = Field(
        description="Versioned arXiv reference URL; not a claim about the download method."
    )
    pdf_origin: dict = Field(
        description="Original source-package provenance, including synthetic or local import origins."
    )
    objects: list[SourceObject]


class CatalogIdentifier(LiteratureIdentifier):
    source: str


class CatalogContributionType(LiteratureContributionType):
    source: str


class CatalogValue(LiteratureValue):
    source: str


class CatalogQuantity(Config):
    quantity: QuantityId
    values: list[CatalogValue] = Field(min_length=1)


class SkyPosition(Config):
    """Coordinates used only for identity matching; no epoch propagation."""

    ra_deg: float = Field(ge=0, lt=360, allow_inf_nan=False)
    dec_deg: float = Field(ge=-90, le=90, allow_inf_nan=False)
    frame: Literal["icrs", "fk5", "fk4"]
    equinox: str | None = None
    evidence: list[dict] = Field(default_factory=list)


class SimbadCandidate(Config):
    main_id: str = Field(min_length=1)
    aliases: list[str]
    position: SkyPosition | None
    fields: dict[str, dict] = Field(
        description="Basic returned fields, including units, errors and references when available."
    )
    photometry: list[dict] = Field(
        description="Returned photometry with explicit band, errors and references when available."
    )


class SimbadQuery(Config):
    method: Literal["identifier", "coordinates"]
    identifier: str | None = None
    position: SkyPosition | None = None
    radius_arcsec: Literal[5.0] = 5.0

    @model_validator(mode="after")
    def valid_query(self):
        if self.method == "identifier":
            if not self.identifier or self.position is not None:
                raise ValueError("An identifier query requires only an identifier.")
        elif self.position is None or self.identifier is not None:
            raise ValueError("A coordinate query requires only a position.")
        return self


class SimbadResponse(Config):
    """A frozen response kept in the private build workspace, not in the catalog."""

    query: SimbadQuery
    queried_at: str
    client_version: str
    service_version: str | None = None
    candidates: list[SimbadCandidate]
    raw_response: str
    error: str | None = None


class SimbadSnapshot(Config):
    status: Literal[
        "not_queried", "matched", "not_found", "ambiguous", "conflict", "failed"
    ]
    method: Literal["identifier", "coordinates"] | None = None
    selected: SimbadCandidate | None = None
    queries: list[dict] = Field(default_factory=list)
    candidates: list[dict] = Field(default_factory=list)
    reason: str | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_selection(self):
        if self.status == "matched":
            if self.selected is None or self.method is None:
                raise ValueError(
                    "A matched snapshot requires its selected object and method."
                )
        elif self.selected is not None:
            raise ValueError("An unresolved snapshot cannot select an object.")
        return self


class AssociationOrigin(Config):
    """Fixed paper identity evidence, including sources superseded by later versions."""

    source: LiteratureMetadata
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pdf_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    pdf_url: str
    pdf_origin: dict
    result_index: int = Field(ge=0)
    identifiers: list[LiteratureIdentifier] = Field(min_length=1)


class Association(Config):
    left: str
    right: str
    method: Literal["identifier", "coordinates", "paper", "simbad", "previous"]
    accepted: bool
    details: dict
    historical: bool = Field(
        default=False,
        description="True for an association adopted or considered in a previous snapshot.",
    )
    origins: dict[str, AssociationOrigin] = Field(
        default_factory=dict,
        description="Identity-only provenance for every paper endpoint, including old versions.",
    )


class CatalogStar(Config):
    schema_version: Literal[1] = 1
    data_version: str
    catalog_id: CatalogId
    display_name: str
    sources: dict[str, CatalogSource] = Field(min_length=1)
    identifiers: list[CatalogIdentifier] = Field(min_length=1)
    contribution_types: list[CatalogContributionType] = Field(min_length=1)
    quantities: list[CatalogQuantity]
    simbad: SimbadSnapshot
    associations: list[Association]
    warnings: list[str]

    @model_validator(mode="after")
    def source_bindings(self):
        entries = [
            *self.identifiers,
            *self.contribution_types,
            *(v for q in self.quantities for v in q.values),
        ]
        if any(item.source not in self.sources for item in entries):
            raise ValueError("Every scientific item requires an embedded source.")
        for key, source in self.sources.items():
            if key != source.arxiv_id + source.version:
                raise ValueError("Source keys must include the exact paper version.")
        if len({q.quantity for q in self.quantities}) != len(self.quantities):
            raise ValueError("A quantity must occur in one group.")
        return self


class PublishedPaper(Config):
    source: CatalogSource
    science: ArtifactRef
    audit: ArtifactRef
    original_audit_sha256: str


class PublishedStar(Config):
    catalog_id: CatalogId
    display_name: str
    file: ArtifactRef
    nodes: list[str]


class CatalogManifest(Config):
    schema_version: Literal[1] = 1
    data_version: str
    rules_version: Literal[1] = 1
    software_version: str
    input_fingerprint: str
    simbad_fingerprint: str
    previous_manifest_sha256: str | None
    papers: dict[str, PublishedPaper]
    stars: list[PublishedStar]
    next_id: int = Field(ge=1)
    retired_ids: dict[CatalogId, list[CatalogId]]
