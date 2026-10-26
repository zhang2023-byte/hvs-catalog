"""Scientific SIMBAD presentation and minimal identity evidence for catalog readers."""

from typing import Literal

from pydantic import Field, JsonValue, model_validator

from .common import ArtifactRef, Config


class SimbadValue(Config):
    value: JsonValue = None
    unit: str | None = None


class SimbadMeasurement(SimbadValue):
    error: JsonValue = None
    error_unit: str | None = Field(
        default=None, description="Present only when the reported error unit differs."
    )
    quality: str | None = None
    bibcode: str | None = None
    wavelength: str | None = None


class SimbadEllipse(Config):
    major: SimbadValue | None = None
    minor: SimbadValue | None = None
    angle: SimbadValue | None = None


class SimbadCoordinates(Config):
    ra: SimbadValue | None = None
    dec: SimbadValue | None = None
    frame: str | None = None
    equinox: str | None = None
    error_ellipse: SimbadEllipse | None = None
    quality: str | None = None
    bibcode: str | None = None
    wavelength: str | None = None


class SimbadProperMotion(Config):
    ra_cosdec: SimbadValue | None = Field(
        default=None, description="Right-ascension proper motion including cos(dec)."
    )
    dec: SimbadValue | None = None
    error_ellipse: SimbadEllipse | None = None
    quality: str | None = None
    bibcode: str | None = None
    wavelength: str | None = None


class SimbadVelocity(Config):
    radial_velocity: SimbadValue | None = None
    redshift: SimbadValue | None = None
    reported_as: str | None = Field(
        default=None,
        description="Original SIMBAD rvz_type, not an inferred conversion.",
    )
    error: SimbadValue | None = Field(
        default=None, description="Original rvz_err and its own returned unit."
    )
    nature: str | None = None
    quality: str | None = None
    bibcode: str | None = None
    wavelength: str | None = None


class SimbadPhotometry(SimbadMeasurement):
    band: str
    system: str | None = Field(
        default=None, description="Original system code: V is Vega, A is AB."
    )
    additional_fields: dict[str, dict] = Field(default_factory=dict)


class SimbadScience(Config):
    main_id: str
    aliases: list[str] = Field(default_factory=list)
    object_type: str | None = None
    coordinates: SimbadCoordinates | None = None
    proper_motion: SimbadProperMotion | None = None
    parallax: SimbadMeasurement | None = None
    velocity: SimbadVelocity | None = None
    spectral_type: SimbadMeasurement | None = None
    photometry: list[SimbadPhotometry] = Field(default_factory=list)
    additional_fields: dict[str, dict] = Field(
        default_factory=dict,
        description="Other nonempty returned fields; unfamiliar scientific columns are never discarded.",
    )


class SimbadCoordinateMatch(Config):
    source_record: str
    separation_arcsec: float
    radius_arcsec: float
    candidate_count: int | None = Field(
        default=None,
        ge=2,
        description="Present when more than one candidate was considered.",
    )
    assumed_frame: str | None = None
    historical: bool = False


class SimbadMatchSummary(Config):
    identifiers: list[str] = Field(default_factory=list)
    coordinates: list[SimbadCoordinateMatch] = Field(default_factory=list)
    candidates: list[str] = Field(
        default_factory=list,
        description="Distinct object names only for unresolved identity.",
    )


class SimbadSummary(Config):
    status: Literal[
        "not_queried", "matched", "not_found", "ambiguous", "conflict", "failed"
    ]
    method: Literal["identifier", "coordinates", "review"] | None = None
    selected: SimbadScience | None = None
    reason: str | None = None
    match: SimbadMatchSummary | None = None
    last_queried_at: str | None = Field(
        default=None,
        description="Latest recorded primary query time; individual times remain in the trace.",
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


class CompactSimbad(SimbadSummary):
    """Legacy version-three summary with its SIMBAD-only provenance reference."""

    provenance: ArtifactRef
