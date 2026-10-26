"""Versioned human review inputs, bound to exact paper and SIMBAD snapshots."""

from datetime import date
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import Config
from .literature_contribution import LiteratureIdentifier, LiteratureValue

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Node = Annotated[str, Field(pattern=r"^.+v[0-9]+#/stars/(0|[1-9][0-9]*)$")]
ValuePath = Annotated[
    str,
    Field(pattern=r"^/stars/[0-9]+/quantities/[0-9]+/values/[0-9]+$"),
]


class ReviewEvidence(Config):
    paper: str = Field(
        description="Exact versioned paper key in the decision bindings."
    )
    location: str = Field(min_length=1)
    quote: str | None = Field(
        default=None, min_length=1, exclude_if=lambda v: v is None
    )


class IdentityDecision(Config):
    id: str = Field(min_length=1)
    type: Literal["merge", "separate", "name", "simbad"]
    groups: list[list[Node]] = Field(
        min_length=1,
        description="Source-record groups to merge or keep apart; name and SIMBAD target one complete group.",
    )
    papers: dict[str, Digest] = Field(min_length=1)
    responses: dict[Digest, Digest] = Field(
        default_factory=dict,
        description="SIMBAD query key to frozen response hash; changes invalidate the decision.",
    )
    reason: str = Field(min_length=1)
    evidence: list[ReviewEvidence] = Field(min_length=1)
    approved_by: str = Field(min_length=1)
    approved_on: date
    unresolved: bool = Field(
        default=False,
        description="A separate or unselected SIMBAD decision may record an unresolved identity.",
    )
    display_name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    main_id: str | None = Field(
        default=None,
        description="For SIMBAD decisions, null revokes the correspondence.",
    )

    @model_validator(mode="after")
    def coherent(self):
        nodes = [n for g in self.groups for n in g]
        if any(not g for g in self.groups) or len(nodes) != len(set(nodes)):
            raise ValueError("Decision groups must be nonempty and disjoint.")
        if set(self.papers) != {n.split("#")[0] for n in nodes}:
            raise ValueError("Every affected paper version requires its science hash.")
        if any(e.paper not in self.papers for e in self.evidence):
            raise ValueError("Decision evidence requires a bound paper version.")
        if self.type in {"merge", "separate"} and len(self.groups) < 2:
            raise ValueError("Merge and separate require at least two groups.")
        if self.type in {"name", "simbad"} and len(self.groups) != 1:
            raise ValueError("Name and SIMBAD decisions target one complete group.")
        if self.unresolved and (
            self.type not in {"separate", "simbad"} or self.main_id is not None
        ):
            raise ValueError(
                "Unresolved identities remain separate or have no SIMBAD selection."
            )
        if (self.type == "name") != bool(
            self.display_name and self.display_name.strip()
        ):
            raise ValueError("Only a name decision requires a nonempty display name.")
        if self.type != "simbad" and self.main_id is not None:
            raise ValueError("Only a SIMBAD decision may select a main ID.")
        if self.aliases and self.type != "name":
            raise ValueError("Only name decisions may add display aliases.")
        if any(not x.strip() for x in self.aliases) or len(set(self.aliases)) != len(
            self.aliases
        ):
            raise ValueError("Display aliases must be nonempty and unique.")
        if self.type == "simbad" and not self.responses:
            raise ValueError("SIMBAD decisions require frozen response bindings.")
        return self


class CorrectionEvidence(Config):
    page: int = Field(ge=1)
    location: str = Field(
        min_length=1, description="Table and row or precise passage in the fixed PDF."
    )
    quote: str = Field(min_length=1)


class CorrectionDecision(Config):
    id: str = Field(min_length=1)
    type: Literal["correct"] = "correct"
    paper: str = Field(pattern=r"^.+v[0-9]+$")
    science_sha256: Digest
    pdf_sha256: Digest
    path: str = Field(
        pattern=r"^/stars/[0-9]+/(identifiers/[0-9]+|quantities/[0-9]+/values/[0-9]+)$"
    )
    original: dict
    replacement: dict | None = Field(
        description="Complete corrected item, or null to withdraw a value."
    )
    evidence: list[CorrectionEvidence] = Field(min_length=1)
    reason: str = Field(min_length=1)
    approved_by: str = Field(min_length=1)
    approved_on: date

    @model_validator(mode="after")
    def content(self):
        model = (
            LiteratureIdentifier if "/identifiers/" in self.path else LiteratureValue
        )
        model.model_validate(self.original)
        if self.replacement is not None:
            model.model_validate(self.replacement)
        elif model is LiteratureIdentifier:
            raise ValueError("Only a value can be withdrawn.")
        if self.original == self.replacement:
            raise ValueError("A correction must change or withdraw the item.")
        return self


ReviewDecision = Annotated[
    IdentityDecision | CorrectionDecision, Field(discriminator="type")
]


class ValueException(Config):
    path: ValuePath
    attribution: Literal["cited", "mismatch"]
    reason: str = Field(min_length=1)


class PaperReview(Config):
    paper: str = Field(pattern=r"^.+v[0-9]+$")
    science_sha256: Digest
    reviewed: Literal[True]
    reason: str = Field(
        min_length=1, description="Review method and any unresolved own defaults."
    )
    approved_by: str = Field(min_length=1)
    approved_on: date
    exceptions: list[ValueException] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_paths(self):
        paths = [e.path for e in self.exceptions]
        if len(paths) != len(set(paths)):
            raise ValueError("A value may have only one review exception.")
        return self


class CatalogReview(Config):
    schema_version: Literal[1, 2] = 2
    decisions: list[ReviewDecision] = Field(default_factory=list)
    papers: list[PaperReview] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_records(self):
        if self.schema_version == 1 and any(
            d.type == "correct" or getattr(d, "aliases", []) for d in self.decisions
        ):
            raise ValueError("Corrections and display aliases require review schema 2.")
        for keys in ([d.id for d in self.decisions], [p.paper for p in self.papers]):
            if len(keys) != len(set(keys)):
                raise ValueError("Duplicate review decision or paper review.")
        return self


class AppliedCorrection(Config):
    """Marks a stellar item replaced by an approved correction; evidence is the fixed PDF."""

    decision: str = Field(min_length=1)
    evidence: list[CorrectionEvidence] = Field(min_length=1)


# Human-edited review directory. The loader expands these files into CatalogReview.

Approval = Annotated[
    str,
    Field(
        pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2} \S",
        description="Approval date followed by the approver or approval record.",
    ),
]
SourcePath = Annotated[
    str,
    Field(
        pattern=r"^.+v[0-9]+#/stars/[0-9]+/(identifiers/[0-9]+|quantities/[0-9]+/values/[0-9]+)$"
    ),
]


class ReviewedPaper(Config):
    paper: str = Field(pattern=r"^.+v[0-9]+$")
    science_sha256: Digest | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="Fixed science hash; bind-review fills a missing value.",
    )
    pdf_sha256: Digest | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="Fixed PDF hash, required only by corrections.",
    )
    values_reviewed: bool = Field(
        default=True,
        exclude_if=lambda v: v,
        description="False binds identity decisions before the value review is complete.",
    )
    note: str | None = Field(default=None, exclude_if=lambda v: v is None, min_length=1)
    approved: Approval | None = Field(default=None, exclude_if=lambda v: v is None)
    exceptions: list[ValueException] = Field(
        default_factory=list, exclude_if=lambda v: not v
    )

    @model_validator(mode="after")
    def unique_paths(self):
        paths = [e.path for e in self.exceptions]
        if len(paths) != len(set(paths)):
            raise ValueError("A value may have only one review exception.")
        if not self.values_reviewed and (self.exceptions or self.note):
            raise ValueError("Unreviewed paper values cannot carry exceptions.")
        return self


class PaperReviews(Config):
    """Binding table and value review: each paper version and its hashes appear once."""

    schema_version: Literal[3] = 3
    method: str = Field(
        min_length=1, description="Review method shared by every paper entry."
    )
    approved: Approval = Field(description="Default approval for paper entries.")
    papers: list[ReviewedPaper] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_papers(self):
        keys = [p.paper for p in self.papers]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate paper review.")
        return self


class IdentityEntry(Config):
    id: str = Field(min_length=1)
    type: Literal["merge", "separate", "name", "simbad"]
    object: str | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="Reading label only; the build ignores it.",
    )
    groups: list[list[Node]] = Field(min_length=1)
    display_name: str | None = Field(default=None, exclude_if=lambda v: v is None)
    aliases: list[str] = Field(default_factory=list, exclude_if=lambda v: not v)
    main_id: str | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="SIMBAD object selected for the group.",
    )
    revoke: bool = Field(
        default=False,
        exclude_if=lambda v: not v,
        description="SIMBAD decision that leaves the group without a correspondence.",
    )
    unresolved: bool = Field(default=False, exclude_if=lambda v: not v)
    responses: dict[Digest, Digest] = Field(
        default_factory=dict,
        exclude_if=lambda v: not v,
        description="SIMBAD query key to response hash; bind-review fills an empty map.",
    )
    reason: str = Field(min_length=1)
    evidence: list[ReviewEvidence] = Field(min_length=1)
    approved: Approval

    @model_validator(mode="after")
    def simbad_selection(self):
        if self.type == "simbad" and (self.main_id is None) != self.revoke:
            raise ValueError("A SIMBAD decision selects main_id or revokes.")
        if self.type != "simbad" and (self.main_id is not None or self.revoke):
            raise ValueError("Only a SIMBAD decision selects or revokes main_id.")
        return self


class IdentityReviews(Config):
    schema_version: Literal[3] = 3
    decisions: list[IdentityEntry] = Field(default_factory=list)


class CorrectionEntry(Config):
    id: str = Field(min_length=1)
    object: str | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="Reading label only; the build ignores it.",
    )
    path: SourcePath
    change: dict[str, list] | None = Field(
        default=None,
        exclude_if=lambda v: v is None,
        description="Changed fields as [extracted, corrected]; null means absent.",
    )
    withdraw: bool = Field(default=False, exclude_if=lambda v: not v)
    evidence: list[CorrectionEvidence] = Field(min_length=1)
    reason: str = Field(min_length=1)
    approved: Approval

    @model_validator(mode="after")
    def one_action(self):
        if self.withdraw == bool(self.change):
            raise ValueError("A correction either changes fields or withdraws a value.")
        if self.withdraw and "/identifiers/" in self.path:
            raise ValueError("Only a value can be withdrawn.")
        for field, pair in (self.change or {}).items():
            if field in {"evidence", "source"} or len(pair) != 2 or pair[0] == pair[1]:
                raise ValueError(
                    "Each change is a differing [extracted, corrected] pair."
                )
        return self


class CorrectionReviews(Config):
    schema_version: Literal[3] = 3
    corrections: list[CorrectionEntry] = Field(default_factory=list)


class StandardValue(Config):
    """Deterministic unit conversion; decimal strings preserve reported precision."""

    unit: Literal["km/s", "kpc", "deg", "mas", "mas/yr", "1"]
    value: str | None = Field(default=None, exclude_if=lambda v: v is None)
    error: str | None = Field(default=None, exclude_if=lambda v: v is None)
    lower_error: str | None = Field(default=None, exclude_if=lambda v: v is None)
    upper_error: str | None = Field(default=None, exclude_if=lambda v: v is None)
    lower_bound: str | None = Field(default=None, exclude_if=lambda v: v is None)
    upper_bound: str | None = Field(default=None, exclude_if=lambda v: v is None)
    lower_inclusive: bool | None = Field(default=None, exclude_if=lambda v: v is None)
    upper_inclusive: bool | None = Field(default=None, exclude_if=lambda v: v is None)
    approximate: bool = False
