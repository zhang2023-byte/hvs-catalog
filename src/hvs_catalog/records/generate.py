"""Generate format documentation, JSON schemas, and English templates from shared
definitions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .common import ArtifactRef, GapRef, ItemRef, RecordRef, SchemaTag
from .objects import (
    ContributionLink,
    ExternalEntry,
    ExternalField,
    ExternalLink,
    ExternalRecordData,
    ObjectChange,
    ObjectData,
)
from .paper import (
    ConditionalAssessment,
    ContributionSummary,
    ContributionTypeEntry,
    CoverageReport,
    DirectEvidencePart,
    ErrorComponent,
    IdentifierEntry,
    MeasuredValue,
    ObjectContribution,
    PaperBoundness,
    PaperData,
    PaperMetadata,
    PrecedentApproval,
    PrecedentSnapshotRef,
    PrecedentUse,
    QuantileReport,
    QuantityConflict,
    QuantityCoverageEntry,
    QuantityGroup,
)
from .questions import (
    Actor,
    DecisionData,
    DecisionPatch,
    PatchChange,
    QuestionData,
    RulesRef,
)
from .registry import RECORD_MODELS, RECORD_TYPE_NAMES
from .roster import (
    RosterCoverage,
    RosterData,
    RosterEntry,
    RosterIdentifier,
    UnresolvedGroup,
)
from .roster_input import roster_input_schema
from .rules import load_rules
from .runs import (
    Approval,
    Attempt,
    Channel,
    InputSnapshot,
    ModelJudgment,
    ModelSpec,
    ReleaseData,
    RunData,
    RunError,
    StepRecord,
    Usage,
)
from .source import (
    AffectedItem,
    AffectedTargets,
    ArxivOrigin,
    DataElementDecoder,
    DataElementLocator,
    DataElementSelector,
    EvidenceData,
    Gap,
    SourceFile,
    SourcePackageData,
    SyntheticOrigin,
    TableCellLocator,
    TextLocator,
)
from .submission import (
    QuantitySubmission,
    ValueSubmission,
)

COMMON_INSTRUCTIONS = (
    "Return only the scientific fields requested for this step. Write new summaries, "
    "questions and precedent notes in English; preserve the paper's printed identifiers, "
    "values, units, conditions and excerpts in their original form. Use only the supplied "
    "paper materials and fixed references. Provide every required list explicitly; an "
    "empty list does not establish that the paper reported nothing. Keep material gaps, "
    "unresolved scientific questions and the authors' assessment distinct. Supply the "
    "evidence required by this step; never invent missing content or silently resolve "
    "conflicting results. Use application-supplied identifiers where provided. The "
    "application creates persistent record identifiers, references and execution "
    "metadata; it does not supply scientific judgments or expert decisions."
)

CROSS_RECORD_RULES = (
    (
        "Reference resolution",
        (
            "Every RecordRef, ItemRef and GapRef must resolve to a record that is present "
            "in the collection at exactly the referenced revision; a missing or stale "
            "revision is rejected instead of silently reading the latest one."
        ),
    ),
    (
        "Typed references",
        (
            "source_ref fields must point to source_package records; roster_ref to roster; "
            "evidence_refs to evidence; question_ref and issue_refs to question; "
            "decision_refs to decision; release source_run_refs to run; object change "
            "before/after refs to object records. Every field that carries scientific "
            "evidence is checked the same way, including identifier, summary, type, "
            "assessment, numeric, roster, question, decision, gap-mention and object "
            "link evidence. Object contribution links must point to a paper record that "
            "contains the referenced item; external links must point to an entry that "
            "exists in the referenced external record."
        ),
    ),
    (
        "Paper consistency",
        (
            "A paper record, its source package, its roster and every piece of evidence "
            "it cites — identifiers, summaries, types, assessments, numeric components "
            "and context evidence, on the exact referenced revision — must share the "
            "same paper_key. Evidence files must belong to the referenced source "
            "package. Question and decision evidence must belong to the paper of the "
            "question's source material."
        ),
    ),
    (
        "Roster binding",
        (
            "Included roster entries and paper object_contributions must correspond one to "
            "one through roster_item_id and contribution_id; excluded or pending entries "
            "carry no contribution."
        ),
    ),
    (
        "Quantity coverage",
        (
            "Each contribution carries exactly one quantity_coverage entry for every "
            "quantity in the approved 18-quantity scope. complete entries cannot "
            "reference material gaps and contradict any gap whose declared impact "
            "covers the entry's object and quantity (an empty item_id or quantity in a "
            "gap target means all objects or the whole object; an unknown scope covers "
            "everything). partial entries must reference every gap affecting the "
            "entry. checked files must exist in the paper's source package. Gap "
            "targets may also name a roster-level judgment — inclusion or identity — "
            "for known objects; such gaps keep the roster coverage unfinished (a "
            "complete roster claim is rejected, a partial one must reference the gap) "
            "while quantity-only known gaps leave the roster unaffected. not_checked "
            "honestly lists no checked files."
        ),
    ),
    (
        "Numeric fragments",
        (
            "Every raw_fragment of a numeric component must equal the saved numeric "
            "string of that component (value, errors, endpoints, coverage, quantiles, "
            "named error components) and occur inside the excerpt of the exact "
            "evidence revision it cites. Recorded numbers are the printed fragments; "
            "the validator never re-computes or re-formats them."
        ),
    ),
    (
        "Conflict exclusivity",
        (
            "A measurement pending in a quantity_conflict cannot also be saved as a "
            "confirmed value of the same quantity, not even under a new value_id or "
            "with the order of any evidence collection permuted — including the "
            "evidence inside named error components and interval reports: identity "
            "is established by the full expression with nested evidence sets "
            "normalized. Independent measurements — different conditions, sources "
            "or evidence — remain unaffected, and no unit conversion or scientific "
            "equivalence is inferred."
        ),
    ),
    (
        "Decision binding",
        (
            "The question's stored fingerprint is recomputed from its actual fixed "
            "content (category, targets, evidence, sources, rules); a question whose "
            "fingerprint does not match its content is rejected. A decision must carry "
            "the recomputed fingerprint of the exact question revision it answers, so "
            "a decision made for an older version of the question cannot be applied."
        ),
    ),
    (
        "Patch validation",
        (
            "A decision's result_patch must target one of the exact question targets "
            "(record, revision, item) that exists in the target record. Fields are "
            "resolved against the actual target item's model (a paper contribution or "
            "a roster entry): record-level fields cannot be reached through an item "
            "target. All changes of one patch are merged into a plain copy of the "
            "item and the final state is parsed once, so legal combined changes "
            "(e.g. excluding an entry and clearing its contribution) pass in any "
            "order while field and cross-field constraints still hold. The patched "
            "item's scientific references then run through the same checks as saved "
            "records — existence at the exact revision, record types, paper "
            "ownership, numeric fragment containment — so a patch cannot bypass the "
            "normal entry points. Patch application is provided by records.patches; "
            "transactional saving and recovery are responsibilities of the workspace "
            "and runtime interfaces."
        ),
    ),
    (
        "Revision history",
        (
            "Records of the same record_id must have strictly increasing unique revisions; "
            "existing revisions are never overwritten in place."
        ),
    ),
    (
        "Material integrity",
        (
            "When a materials root is supplied, package file hashes, external raw responses "
            "and evidence locators (text line ranges and excerpts, table cells, JSON "
            "pointers and PDF text blocks) are verified against the actual material; a changed hash or a "
            "mismatched excerpt is rejected. A locator whose selector kind has no "
            "verifier is reported as unverified and fails validation — unsupported "
            "locators never silently count as verified. For pdf_image evidence, only the "
            "PDF/page/block binding and image files are validated: the excerpt is the "
            "extractor's visual transcription, not automatically verified text."
        ),
    ),
    (
        "Release binding",
        (
            "The manifest hash is recomputed from the actual release content (data "
            "version, content refs, artifacts, source runs, code version, change "
            "summary, channel targets) and must match the stored manifest_sha256. A "
            "non-null approval must bind the approver, time, the recomputed manifest "
            "hash and a declared channel target; a failed channel cannot be reported "
            "as succeeded and success requires a receipt reference."
        ),
    ),
)

MODEL_SECTIONS: tuple[tuple[str, tuple[type[BaseModel], ...]], ...] = (
    (
        "Common reference types",
        (SchemaTag, RecordRef, ItemRef, ArtifactRef, GapRef),
    ),
    (
        "Source package and evidence",
        (
            SyntheticOrigin,
            ArxivOrigin,
            SourceFile,
            AffectedItem,
            AffectedTargets,
            Gap,
            SourcePackageData,
            TextLocator,
            TableCellLocator,
            DataElementSelector,
            DataElementDecoder,
            DataElementLocator,
            EvidenceData,
        ),
    ),
    (
        "Paper and object contributions",
        (
            PaperMetadata,
            DirectEvidencePart,
            CoverageReport,
            QuantileReport,
            ErrorComponent,
            MeasuredValue,
            IdentifierEntry,
            ContributionSummary,
            ContributionTypeEntry,
            PaperBoundness,
            ConditionalAssessment,
            QuantityGroup,
            QuantityConflict,
            QuantityCoverageEntry,
            PrecedentApproval,
            PrecedentSnapshotRef,
            PrecedentUse,
            ObjectContribution,
            PaperData,
        ),
    ),
    (
        "Roster",
        (RosterCoverage, RosterIdentifier, RosterEntry, UnresolvedGroup, RosterData),
    ),
    (
        "Objects and external records",
        (
            ContributionLink,
            ExternalLink,
            ObjectChange,
            ObjectData,
            ExternalField,
            ExternalEntry,
            ExternalRecordData,
        ),
    ),
    (
        "Questions and decisions",
        (RulesRef, QuestionData, Actor, PatchChange, DecisionPatch, DecisionData),
    ),
    (
        "Run and release manifests",
        (
            Usage,
            ModelSpec,
            InputSnapshot,
            Attempt,
            StepRecord,
            RunError,
            ModelJudgment,
            RunData,
            Approval,
            Channel,
            ReleaseData,
        ),
    ),
)


def _cell(text: Any) -> str:
    return str(text).replace("|", r"\|").replace("\n", " ")


def _type_text(prop: dict[str, Any]) -> str:
    if "$ref" in prop:
        name = prop["$ref"].rsplit("/", 1)[-1]
        return f"`{name}`"
    for union in ("anyOf", "oneOf"):
        if union in prop:
            return " | ".join(_type_text(option) for option in prop[union])
    if "const" in prop:
        return f"`{prop['const']}`"
    if "enum" in prop:
        return " / ".join(f"`{value}`" for value in prop["enum"])
    kind = prop.get("type", "value")
    if kind == "array":
        items = prop.get("items", {})
        return f"list of {_type_text(items)}"
    return f"`{kind}`"


def _constraints(prop: dict[str, Any]) -> str:
    notes = []
    if "minLength" in prop:
        notes.append(f"minLength {prop['minLength']}")
    if "minItems" in prop:
        notes.append(f"minItems {prop['minItems']}")
    if "pattern" in prop:
        notes.append(f"pattern `{prop['pattern']}`")
    if "minimum" in prop:
        notes.append(f"minimum {prop['minimum']}")
    if "exclusiveMinimum" in prop:
        notes.append(f"> {prop['exclusiveMinimum']}")
    return "; ".join(notes)


def _model_table(model: type[BaseModel]) -> list[str]:
    schema = model.model_json_schema()
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    lines = [f"#### {model.__name__}", ""]
    if schema.get("description"):
        lines += [schema["description"], ""]
    lines += [
        "| Field | Type | Required | Constraints | Notes |",
        "|---|---|---|---|---|",
    ]
    for name, prop in properties.items():
        required_mark = "yes" if name in required else "no"
        description = prop.get("description", "")
        notes = _cell(description) if description else ""
        if prop.get("nullable") or (
            "anyOf" in prop
            and any(option.get("type") == "null" for option in prop["anyOf"])
        ):
            notes = (
                notes + " " if notes else ""
            ) + "Nullable: absent content is `null`, never a default value."
        lines.append(
            f"| `{name}` | {_cell(_type_text(prop))} | {required_mark} | {_cell(_constraints(prop))} | {notes} |"
        )
    lines.append("")
    return lines


def _envelope_section() -> list[str]:
    lines = [
        "### Record envelope",
        "",
        (
            "Every record is a JSON object with the following outer fields; the payload "
            "lives under `data`. Field names, status codes and this documentation are "
            "English. New summaries, questions and precedent notes are English; "
            "printed identifiers, values and evidence excerpts retain their original form."
        ),
        "",
        "| Field | Type | Required | Notes |",
        "|---|---|---|---|",
        "| `schema` | `{name, version}` | yes | `name` is one of the record types below; `version` is `1`. Unknown names or versions are rejected. |",
        "| `record_id` | string | yes | Stable technical identifier; revisions of one record share it. |",
        "| `revision` | integer >= 1 | yes | Strictly increasing per record_id; revisions are never overwritten in place. |",
        "| `created_at` | timestamp | yes | Must carry a timezone. |",
        "| `execution_id` | string, nullable | yes | Links the creating execution; null only for manual imports. |",
        "| `data` | record payload | yes | Typed by `schema.name`; undeclared fields are rejected. |",
        "",
        "| Record type (`schema.name`) | Envelope model |",
        "|---|---|",
    ]
    for name in RECORD_TYPE_NAMES:
        lines.append(f"| `{name}` | `{RECORD_MODELS[name].__name__}` |")
    lines.append("")
    return lines


def _quantity_tables() -> list[str]:
    rules = load_rules()
    lines = [
        "### Quantity scope (18 quantities)",
        "",
        (
            "The quantity code is the legacy path of the approved science rules; the "
            "definitions below are generated from the same source."
        ),
        "",
        "| Code | Name | Definition |",
        "|---|---|---|",
    ]
    for quantity in rules.quantities:
        lines.append(
            f"| `{quantity.legacy_path}` | {_cell(quantity.name)} | {_cell(quantity.definition)} |"
        )
    lines += [
        "",
        "### Authors' overall assessment states (S04)",
        "",
        "| State | Definition |",
        "|---|---|",
    ]
    for state in rules.states:
        lines.append(f"| `{state.id}` | {_cell(state.definition)} |")
    lines += ["", f"Rules version: `{rules.version}` (sha256 `{rules.sha256}`).", ""]
    return lines


def _expansion_section() -> list[str]:
    optional_fields = [
        name
        for name, field in ValueSubmission.model_fields.items()
        if not field.is_required()
    ]
    required_fields = [
        name
        for name, field in ValueSubmission.model_fields.items()
        if field.is_required()
    ]
    optional = ", ".join(f"`{name}`" for name in optional_fields)
    required = ", ".join(f"`{name}`" for name in required_fields)
    return [
        "## Filling formats and expansion rules",
        "",
        (
            "Step submissions contain only the scientific fields of that step. The "
            "application supplies fixed paper and source context, object identifiers, "
            "record identifiers, references, revisions and all execution metadata; none "
            "of these are model-filled. The common instruction text sent with every step "
            "is:"
        ),
        "",
        f"> {COMMON_INSTRUCTIONS}",
        "",
        "Numeric filling rules:",
        "",
        f"- Always provided by the extractor: {required}.",
        (
            f"- Omittable when not applicable: {optional}. The application fills omitted "
            "fields with `null` (or an empty list for `error_components`); this only "
            "marks the field as not provided and implies no scientific conclusion."
        ),
        (
            "- Reported single-sided errors, zeros, negative values, percentages and "
            "statistical interpretations must be preserved; a pure interval carries "
            "endpoints and an `interval_kind` but never a computed central value."
        ),
        (
            "- Evidence is given inline as `{source, parts, context}`. The application "
            "validates each locator and excerpt, creates evidence records, assigns "
            "identifiers, and merges only evidence that is identical under the same "
            "fixed source version. Extractors never maintain evidence keys or persistent "
            "identifiers. For pdf_image evidence, validation checks only the image binding "
            "and file integrity; the quoted visual transcription is not string-verified."
        ),
        (
            "- Coverage is declared once per object as `{scope: all_requested, common, "
            "exceptions}` and expanded deterministically to one entry per requested "
            "quantity. Missing declarations, unknown or duplicate exceptions, unknown "
            "files, gaps or questions, and complete declarations that contradict known "
            "material gaps are rejected. Gaps whose impact scope is unknown block "
            "complete declarations for every quantity."
        ),
        (
            "- Each explicit numeric conflict generates a question record of category "
            "`numeric_conflict` bound to the affected object; the application assigns "
            "identifiers and never answers or closes questions itself."
        ),
        (
            "- A technical failure produces no success-shaped result: the failed step "
            "keeps its error records and previously saved valid results, and no empty "
            "measurement, roster or coverage entry is fabricated."
        ),
        "",
    ]


def render_format_document() -> str:
    lines = [
        "<!-- Generated -->",
        "# Data record format",
        "",
        (
            "This document is generated from the shared Pydantic definitions in "
            "`hvs_catalog.records`. Do not edit it by hand; regenerate with "
            "`uv run python tools/render_record_contracts.py`. JSON Schemas for every "
            "record type and step submission live under `schemas/`; step templates for "
            "extractors live under `templates/`."
        ),
        "",
    ]
    lines += _envelope_section()
    lines += _quantity_tables()
    lines += [
        "## Field definitions",
        "",
        (
            "Required nullable fields must be present with `null`; only fields marked "
            "optional may be omitted in step submissions. List fields state their minimum "
            "item count; a required list must be provided explicitly even when empty."
        ),
        "",
    ]
    for title, models in MODEL_SECTIONS:
        lines += [f"### {title}", ""]
        for model in models:
            lines += _model_table(model)
    lines += [
        "## Cross-record rules",
        "",
        (
            "The checks below cannot be expressed as field constraints alone; they are "
            "implemented in `hvs_catalog.records.validate` and run over whole record "
            "collections."
        ),
        "",
    ]
    for title, text in CROSS_RECORD_RULES:
        lines += [f"- **{title}**: {text}", ""]
    lines += _expansion_section()
    return "\n".join(lines)


def _provided_and_saved(rows: list[tuple[str, str]]) -> list[str]:
    lines = [
        "| Filled by the extractor | Provided or assigned by the application |",
        "|---|---|",
    ]
    lines += [f"| {left} | {right} |" for left, right in rows]
    lines.append("")
    return lines


def render_roster_template() -> str:
    rules = load_rules()
    return "\n".join(
        [
            "<!-- Generated -->",
            "# Roster extraction filling format",
            "",
            (
                "Fill this format for the roster extraction step. "
                "All scientific content is in English; object names, values and excerpts "
                "keep the paper's original language."
            ),
            "",
            "## Provided by the application",
            "",
            (
                "Fixed paper and source context (paper key, source package files with "
                "identifiers, known material gaps with their identifiers, existing open "
                "questions with their identifiers), and all record "
                "identifiers, numbering, revisions and execution metadata."
            ),
            "",
            "## Filled by the extractor",
            "",
        ]
        + _provided_and_saved(
            [
                (
                    "Roster material coverage: state, checked file identifiers, known gap identifiers",
                    "File identifiers, gap identifiers, question identifiers, record references",
                ),
                (
                    (
                        "Entries: printed identifiers with inline evidence, disposition "
                        "(`included` / `excluded` / `pending`), inclusion or exclusion evidence"
                    ),
                    "Roster entry numbering (`item_id`), contribution numbering",
                ),
                (
                    (
                        "For included entries: contribution summary, contribution type "
                        "(`candidates_found` / `follow_up`), the authors' overall boundness "
                        f"assessment (one of {len(rules.states)} states or `null`), "
                        "conditional assessments, each with inline evidence"
                    ),
                    "Question records for pending items and undetermined assessments",
                ),
                (
                    (
                        "Unresolved printed ranges: original range expression, evidence, "
                        "already identified members, reason"
                    ),
                    "Group identifiers; no members are generated by the application",
                ),
            ]
        )
        + [
            "## Citation and entry shape",
            "",
            "Each citation is `{location, quote}`: select a supplied T: text location or I: image location and transcribe the original text. Do not supply file IDs, pages, locator wrappers, parts, or context flags inside citations. The application binds them to the fixed PDF.",
            "Write summary, summary_evidence, contribution_type, type_evidence, boundness, boundness_evidence, conditional_assessments directly on an included entry. There is no contribution wrapper. When needed, use boundness_question or boundness_gap_ids for an unresolved assessment.",
            "In a correction turn, use only the supplied repair_roster fields R1, R2, etc. They refer to application-selected fields, not names of objects. All other content is retained automatically. Do not resubmit the roster.",
            "",
            "## Required behavior",
            "",
            (
                "- `included` entries require inclusion evidence and full contribution "
                "fields; `excluded` entries require exclusion evidence; `pending` entries "
                "require a submitted question or a known material gap. Excluded and pending entries never carry "
                "contribution fields."
            ),
            (
                "- An assessment with status `null` must submit a question or reference a "
                "known material gap; it must not be filled as "
                "`no_overall_conclusion`."
            ),
            (
                "- `no_overall_conclusion` is a reading of the paper; the other four "
                "states require positive evidence inline."
            ),
            (
                "- Interpret clearly identified numbered object ranges and submit each member "
                "as a separate entry with the original range as evidence. Do not expand "
                "individual names or physical numerical intervals. Preserve unidentified "
                "remainders as unresolved groups; never invent unnamed members."
            ),
            (
                "- A known material gap that names an inclusion or identity judgment "
                "keeps the roster coverage partial with that gap referenced; only "
                "quantity-scoped known gaps can leave roster coverage complete, and "
                "an unknown-scope gap always keeps it partial. A complete roster "
                "claim never contradicts an open gap."
            ),
            (
                "- Every identifier must appear in exactly one entry; if two printed "
                "names may denote the same object, apply the active ambiguity policy instead of merging "
                "silently."
            ),
            "",
            "## Common instructions",
            "",
            f"> {COMMON_INSTRUCTIONS}",
            "",
            "The complete field structure is defined by `schemas/submission.roster.json`.",
            "",
        ]
    )


def render_quantity_template() -> str:
    rules = load_rules()
    quantities = ", ".join(f"`{q.legacy_path}`" for q in rules.quantities)
    return "\n".join(
        [
            "<!-- Generated -->",
            "# Quantity extraction filling format",
            "",
            (
                "Fill this format for the quantity extraction step, for each "
                "application-supplied object of the paper."
            ),
            "",
            "## Provided by the application",
            "",
            (
                "Fixed paper and source context, the object identifiers of the current "
                "roster, the requested quantity scope, known material gaps with "
                "identifiers and affected targets, existing open questions with "
                "identifiers, approved precedent snapshots, and all record identifiers, "
                "value numbering, revisions, call states and usage."
            ),
            "",
            "## Filled by the extractor",
            "",
        ]
        + _provided_and_saved(
            [
                (
                    (
                        "Per object: reported quantity groups with values in their "
                        "printed form (value, errors, unit, condition, frame, source), "
                        "inline evidence grouped by source location"
                    ),
                    "Value, conflict, question, evidence and record identifiers",
                ),
                (
                    (
                        "Numeric conflicts: both alternatives in full with evidence and "
                        "the unresolved reason"
                    ),
                    "A `numeric_conflict` question bound to the object and quantity",
                ),
                (
                    (
                        "Coverage declaration: `scope: all_requested`, common state, "
                        "checked files, known gap and question references, plus a "
                        "complete replacement for each exceptional quantity"
                    ),
                    "Expansion to one completion entry for each of the 18 requested quantities",
                ),
                (
                    (
                        "Other scientific questions inline: affected object and quantity, "
                        "category, content, evidence"
                    ),
                    "Question records; the application never answers them",
                ),
                (
                    (
                        "Precedent use: the given precedent identifier and a short "
                        "applicability note"
                    ),
                    "Precedent snapshot references and version checks",
                ),
            ]
        )
        + [
            "## Requested quantity scope",
            "",
            f"The requested quantities are exactly the approved 18: {quantities}.",
            "",
            "## Required behavior",
            "",
            (
                "- Top-level lists `objects` and `questions`, and each object's "
                "`quantities` and `quantity_conflicts` lists, must be present even when "
                "empty. Each object also requires `object_id` and a `coverage` "
                "declaration object, not a list. `questions` belongs at the top level, "
                "not inside an object; an empty list never means the paper reported nothing."
            ),
            (
                "- Omit a numeric group entirely when there is no result; never create a "
                "group with an empty `values` list."
            ),
            (
                "- Provide optional numeric fields (interval endpoints, inclusiveness, "
                "interval kind, coverage, quantiles, error components, frame, epoch, "
                "coordinate format, representation, purpose, source note, author usage) "
                "exactly when the paper reports them."
            ),
            (
                "- The coverage declaration must be explicit. Declare the common state "
                "for all requested quantities and give a complete replacement entry for "
                'every exception, e.g. "all checked in the main text; radial velocity '
                'exceptional, the supplement is not yet processed". Declaring all '
                "quantities not checked is also a valid explicit declaration. "
                "Declarations contradicting known material gaps are rejected, and gaps "
                "with unknown impact scope block complete declarations everywhere."
            ),
            (
                "- Cite every filled numeric component (value, each error side, both "
                "endpoints, coverage, quantiles) with its printed fragment; group "
                "components that share one source location into a single inline "
                "evidence item and mark it as context evidence when it also supports "
                "the condition or source."
            ),
            (
                "- Conflicting printings of one result are reported as a conflict with "
                "at least two distinct complete alternatives; the alternatives are not "
                "simultaneously reported as confirmed values. Other confirmed results "
                "of the paper continue unaffected when the conflict affects neither "
                "inclusion nor identity."
            ),
            (
                "- Use only application-supplied object identifiers; unknown or missing "
                "objects are rejected."
            ),
            "",
            "## Common instructions",
            "",
            f"> {COMMON_INSTRUCTIONS}",
            "",
            (
                "The complete field structure is defined by "
                "`schemas/submission.quantity.json`."
            ),
            "",
        ]
    )


def render_index() -> str:
    schema_names = "".join(
        f"- `schemas/record.{name}.json` — `{name}` records\n"
        for name in RECORD_TYPE_NAMES
    )
    return f"""<!-- Generated -->
# Generated data contracts

Shared record contracts are generated from `hvs_catalog.records`.
The [Gold contracts](gold/README.md) in `gold/` are generated from
`hvs_catalog.evaluation`. Regenerate both with
`uv run python tools/render_record_contracts.py` and check with
`--check`. Do not edit by hand.

- `record-format.md` — full record format documentation
- `schemas/submission.roster.json`, `schemas/submission.quantity.json` — step filling formats
- `templates/roster-extraction.md`, `templates/quantity-extraction.md` — English step filling templates
  covering extractor and application responsibilities. Prompt assembly sends only
  scientific filling sections and the corresponding submission schema.

Per record type:

{schema_names}"""


def _schema_json(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(), indent=2, ensure_ascii=False) + "\n"


def build_outputs() -> dict[str, str]:
    outputs: dict[str, str] = {
        "README.md": render_index(),
        "record-format.md": render_format_document(),
        "templates/roster-extraction.md": render_roster_template(),
        "templates/quantity-extraction.md": render_quantity_template(),
        "schemas/submission.roster.json": json.dumps(
            roster_input_schema(), ensure_ascii=False, indent=2
        )
        + "\n",
        "schemas/submission.quantity.json": _schema_json(QuantitySubmission),
    }
    for name, model in RECORD_MODELS.items():
        outputs[f"schemas/record.{name}.json"] = _schema_json(model)
    return outputs


def write_outputs(root: Path) -> dict[str, str]:
    outputs = build_outputs()
    for relative, content in outputs.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return outputs


#: Independently maintained Gold subtrees are not unexpected outputs of this generator.
_FOREIGN_SUBTREES = frozenset({"gold"})


def check_outputs(root: Path) -> list[str]:
    """Return files differing from generated results; empty means synchronized.

    hvs_catalog.evaluation.generate maintains/checks gold/ separately, so it is never
    unexpected here.
    """
    expected = build_outputs()
    problems: list[str] = []
    for relative, content in expected.items():
        target = root / relative
        if not target.is_file():
            problems.append(f"缺失：{relative}")
        elif target.read_text(encoding="utf-8") != content:
            problems.append(f"过期：{relative}")
    for path in sorted(root.rglob("*")):
        if path.is_file():
            relative = str(path.relative_to(root))
            if relative.split("/", maxsplit=1)[0] in _FOREIGN_SUBTREES:
                continue
            if relative not in expected:
                problems.append(f"多余：{relative}")
    return problems
