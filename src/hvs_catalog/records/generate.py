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
from .quantity_input import quantity_input_schema
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
    ValueSubmission,
)

COMMON_INSTRUCTIONS = (
    "Return only the scientific fields requested for this step. Write new summaries, "
    "interpretation rationales and diagnostic reasons in English. Record identifiers "
    "as specified in S02; preserve values, units, conditions and excerpts in their "
    "original form. Use only supplied "
    "paper materials. Provide required lists explicitly; an empty list does not establish "
    "that the paper reported nothing. Keep material gaps, parser errors, source errors, "
    "conflicting measurements and the authors' assessment distinct. Choose the "
    "best-supported interpretation under S07 and briefly record its evidence-based rationale. "
    "Source errors require original-paper evidence, definitions or explicit logical "
    "contradictions; unusual but possible values are not errors. Preserve errors and "
    "unexplained same-condition conflicts as diagnostics, omit affected claims, and "
    "continue independent results. Never invent a replacement value. Supply required "
    "evidence and use application-supplied identifiers. The application assigns "
    "persistent record identifiers, references and execution metadata."
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
            "honestly lists no checked files. Current source-error and same-condition "
            "conflict diagnostics prevent complete coverage for the affected quantity."
        ),
    ),
    (
        "Numeric fragments",
        (
            "Every raw_fragment of a numeric component must equal the saved numeric "
            "string of that component (value, errors, endpoints, coverage, quantiles, "
            "named error components). Its evidence reference must resolve to the correct "
            "paper and revision. Literal containment in the excerpt is not checked; "
            "scientific support and parsing fidelity remain unverified. Recorded numbers "
            "retain the extractor's reported expressions; "
            "the validator never re-computes or re-formats them."
        ),
    ),
    (
        "Conflict exclusivity",
        (
            "A current passive conflict or source error blocks confirmed measurements "
            "of that quantity under the affected condition, while other conditions "
            "continue. In historical question-backed conflicts, a measurement cannot also be saved as a "
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
            "ownership, numeric component evidence references — so a patch cannot bypass the "
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
            "Record identifiers as specified in S02; values and evidence excerpts "
            "retain their original form."
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
            "- Current numeric conflicts and source errors are passive diagnostics bound "
            "to the affected object and quantity. Their values never become formal "
            "measurements and their quantity coverage stays partial. Historical "
            "question-backed conflicts remain readable in their original format."
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
                "identifiers and known material gaps with their identifiers), and all record "
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
                    "File identifiers, gap identifiers, record references",
                ),
                (
                    (
                        "Entries: identifiers as specified in S02 with inline evidence, disposition "
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
                    "Bound diagnostic identifiers; no human tasks",
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
            "Write summary, summary_evidence, contribution_type, type_evidence, boundness, boundness_evidence, conditional_assessments directly on an included entry. There is no contribution wrapper. Use boundness_author_errors or boundness_gap_ids for an assessment blocked by source errors or missing material.",
            "In a correction turn, use only the supplied repair_roster fields R1, R2, etc. They refer to application-selected fields, not names of objects. All other content is retained automatically. Do not resubmit the roster.",
            "",
            "## Required behavior",
            "",
            (
                "- `included` entries require inclusion evidence and full contribution "
                "fields; `excluded` entries require exclusion evidence; `pending` entries "
                "require source-error diagnostics or a known material gap. Excluded and pending entries never carry "
                "contribution fields."
            ),
            (
                "- An assessment with status `null` must report source-error diagnostics or reference a "
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
                "names may denote the same object, choose the best-supported interpretation under S07 rather than merging "
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
                "identifiers and affected targets, and all record identifiers, "
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
                        "For the fixed object: a flat measurements list, each with its quantity and values in their "
                        "printed form (value, errors, unit, condition, frame, source), "
                        "citations using supplied location identifiers and verbatim quotes"
                    ),
                    "Value, conflict, diagnostic, evidence and record identifiers",
                ),
                (
                    (
                        "Numeric conflicts: both alternatives in full with evidence and "
                        "the unresolved reason"
                    ),
                    "Passive conflict diagnostics bound to the object and quantity",
                ),
                (
                    (
                        "Coverage declaration: common state, plus quantity and state "
                        "for each exception. Do not fill technical file or gap IDs."
                    ),
                    "Fixed supplied file binding, material gaps and diagnostic coverage; expansion to all 18 requested quantities",
                ),
                (
                    (
                        "Source errors: affected quantity, condition and source; frame or epoch when applicable; original raw_values, reason and evidence"
                    ),
                    "Passive source-error diagnostics",
                ),
                (
                    (
                        "Interpretation rationale in model_judgments, citing supplied locations"
                    ),
                    "Run judgment records",
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
                "- Exactly one application-supplied object is processed per call. "
                "Provide top-level `measurements` and `quantity_conflicts` "
                "lists even when empty, plus a `coverage` declaration object. "
                "Each measurements entry contains `quantity` and one complete numeric "
                "expression directly (e.g. value, error, condition, source, evidence). "
                "Repeat entries for distinct results. Do not submit objects, object_id, "
                "quantity groups, or nested values lists; the application binds and groups them. "
                "An empty list never means the paper reported nothing."
            ),
            (
                "- Omit a measurement entirely when there is no result; never create "
                "an empty numeric expression."
            ),
            (
                "- A one-sided lower or upper limit uses value for its printed operand; "
                "range_lower and range_upper describe a two-sided range. Preserve the "
                "explicit inequality and its printed number."
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
                "Use {common: {state: ...}, exceptions: [{quantity: ..., state: ...}]}. "
                "The scope is all requested quantities. The application assigns checked files "
                "from the supplied PDF whenever the state indicates checking, and binds known "
                "material gaps and diagnostics using their affected quantities. Do not "
                "supply scope, checked_file_ids, gap_ids or issue_ids in coverage. "
                "Declarations contradicting known material gaps are rejected, and gaps "
                "with unknown impact scope block complete declarations everywhere."
            ),
            (
                "- Cite evidence with location and quote only for each measurement and "
                "nested numerical structure. Text quotes must match the fixed parser text. "
                "Literal numeric containment is not checked; this does not certify that "
                "the cited passage supports the filled number. The application "
                "assigns component names and evidence references; do not submit parts, "
                "file identifiers, locators or context flags. Include condition and source "
                "support in the cited passages. Preserve math notation and signed strings "
                "exactly as printed; do not rewrite them into decimal approximations."
            ),
            (
                "- Unexplained conflicting printings of one result under the same condition are reported as a conflict with "
                "at least two distinct complete alternatives; the alternatives are not "
                "simultaneously reported as confirmed values. Different conditions are separate "
                "measurements, not conflicts. Report clear source errors in author_errors with "
                "quantity, condition, source, frame/epoch when applicable, raw_values, evidence and reason; never guess a correction. "
                "Affected coverage is partial, not unreported. Other confirmed results "
                "of the paper continue unaffected when the conflict affects neither "
                "inclusion nor identity."
            ),
            (
                "- All measurements, conflicts and source-error diagnostics belong to the "
                "single supplied target. The application assigns its identifier; "
                "do not repeat object_id in the submission."
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
        "schemas/submission.quantity.json": json.dumps(
            quantity_input_schema(), ensure_ascii=False, indent=2
        )
        + "\n",
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
