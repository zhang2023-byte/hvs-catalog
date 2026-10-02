"""Generate Gold documentation, JSON schemas, and annotation templates from typed
definitions.

Write contracts/generated/gold/ through tools/render_record_contracts.py
rendering/checking; never hand-edit outputs. Also generate legacy v4 adapter schemas to
document migration input scope.
"""

from __future__ import annotations

import json
from inspect import cleandoc
from pathlib import Path
from typing import Any, get_args, get_origin

from pydantic import BaseModel

from ..records.rules import load_rules
from . import models as models_module
from .compact import (
    COMPACT_METADATA_SCHEMA_NAME,
    COMPACT_SCHEMA_VERSION,
    CompactAnnotationActors,
    CompactBoundness,
    CompactClaim,
    CompactConflict,
    CompactContribution,
    CompactCoverage,
    CompactErrorComponent,
    CompactExclusion,
    CompactGoldAnswer,
    CompactGoldMetadata,
    CompactIdentifier,
    CompactPaper,
    CompactQuantityGroup,
    CompactReview,
    CompactReviewConfirmation,
    CompactRevision,
    CompactScopeLimitation,
    CompactUnresolved,
    CompactValue,
    compact_answer_to_json,
    compact_metadata_to_json,
)
from .legacy import (
    LEGACY_AUTHOR_STATUSES,
    LEGACY_CONTRIBUTION_TYPES,
    LEGACY_COORDINATE_FORMATS,
    LEGACY_INTERVAL_KINDS,
    LEGACY_LIMIT_KINDS,
    LEGACY_REPRESENTATIONS,
    LEGACY_SCHEMA_NAME,
    LEGACY_SCHEMA_VERSION,
    LegacyAnnotation,
)
from .models import (
    GOLD_COORDINATE_FORMATS,
    GOLD_INTERVAL_KINDS,
    GOLD_LIMIT_KINDS,
    GOLD_REPRESENTATIONS,
    GOLD_SCHEMA_NAME,
    GOLD_SCHEMA_VERSION,
    GoldAnnotation,
)
from .store import SaveApproval

GOLD_OUTPUT_DIRNAME = "gold"

_ANNOTATION_INSTRUCTIONS = (
    "Annotate one paper from its fixed PDF. Record the paper's own judgments and "
    "printed values verbatim: keep identifiers, numeric strings, units, conditions "
    "and error forms exactly as printed; never convert, complete, or average them. "
    "Cite where in the PDF you saw each statement using the registered PDF file, a "
    "free-text location description, an optional 1-based PDF page ordinal (the "
    "position in the file, not the printed page number), and an optional verbatim "
    "quote. Bind every piece of evidence to a registered file with its content "
    "hash. Mark unresolved science (author status, competing values) as pending "
    "with a reason instead of inventing an answer. Technical identifiers "
    "(contribution, conflict, exclusion, pending ids) are assigned by the tool; "
    "supply the scientific content and known attribution/material facts explicitly."
)


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _type_text(annotation: Any) -> str:
    origin = get_origin(annotation)
    if origin is None:
        if annotation is type(None):
            return "null"
        if hasattr(annotation, "__name__"):
            if annotation is str:
                return "string"
            if annotation is int:
                return "integer"
            if annotation is bool:
                return "boolean"
            return annotation.__name__
        return str(annotation)
    args = get_args(annotation)
    if origin is type(None):
        return "null"
    inner = " | ".join(sorted({_type_text(arg) for arg in args}))
    if origin is list:
        return f"array of {inner}"
    if origin is dict:
        return "object"
    return inner


def _constraints(prop: dict[str, Any]) -> str:
    parts: list[str] = []
    if "minLength" in prop:
        parts.append(f"min length {prop['minLength']}")
    if "pattern" in prop:
        parts.append(f"pattern `{prop['pattern']}`")
    if "minimum" in prop:
        parts.append(f"≥ {prop['minimum']}")
    if prop.get("minItems"):
        parts.append(f"≥ {prop['minItems']} items")
    return "; ".join(parts)


def _schema_properties(model: type[BaseModel]) -> dict[str, dict[str, Any]]:
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    properties = dict(schema.get("properties", {}))
    for value in properties.values():
        ref = value.get("$ref")
        if isinstance(ref, str):
            definition = defs.get(ref.removeprefix("#/$defs/"), {})
            value.update(definition.get("properties", {}))
    return properties


def _field_rows(model: type[BaseModel]) -> list[tuple[str, str, str, str, str]]:
    rows: list[tuple[str, str, str, str, str]] = []
    properties = _schema_properties(model)
    for name, field in model.model_fields.items():
        external = field.alias if (field.alias and field.alias != name) else name
        if field.is_required():
            default = "—"
        elif field.default_factory is not None:
            default = json.dumps(
                field.default_factory(), default=lambda v: v.model_dump(mode="json")
            )
        else:
            default = json.dumps(field.default, ensure_ascii=False)
        rows.append(
            (
                external,
                _type_text(field.annotation),
                "required" if field.is_required() else "optional",
                _cell(default) if default != "" else "",
                _constraints(properties.get(external, {})),
            )
        )
    return rows


def _model_table(model: type[BaseModel]) -> list[str]:
    title = model.__name__
    rows = _field_rows(model)
    lines = [f"#### `{title}`", ""]
    doc = cleandoc(model.__doc__ or "")
    if doc and doc.isascii():
        lines += [doc, ""]
    lines += [
        "| Field | Type | Presence | Default | Constraints |",
        "|---|---|---|---|---|",
    ]
    for name, kind, presence, default, constraints in rows:
        constraint_text = f" {constraints}" if constraints else ""
        lines.append(
            f"| `{name}` | {_cell(kind)} | {presence} | {_cell(default)} |"
            f"{_cell(constraint_text).strip()} |"
        )
    lines.append("")
    return lines


def render_gold_format_document() -> str:
    rules = load_rules()
    lines = [
        "<!-- Generated -->",
        "# Gold v1 working annotation format (compatibility)",
        "",
        (
            f"This compatibility document is generated from the typed definitions in "
            f"`hvs_catalog.evaluation.models` for `{GOLD_SCHEMA_NAME}` "
            f"v{GOLD_SCHEMA_VERSION} and the legacy v4 reader in "
            "`hvs_catalog.evaluation.legacy`. Do not edit it by hand; regenerate "
            "with `uv run python tools/render_record_contracts.py`."
        ),
        "",
        (
            "Current formal Gold uses the two-file v2 answer and metadata contract in "
            "[current Gold format](../gold-format.md). This v1 format remains readable "
            "for migration comparison and previously saved synthetic/historical records."
        ),
        "",
        "## Scope",
        "",
        (
            "The v1 working annotation captures expert judgments with PDF evidence; it "
            "shares the science vocabulary with prediction records but does not "
            "carry run fields (execution ids, framework progress, or automated "
            "extraction methods). A document is either a `draft` (may miss "
            "information required of a formal answer; explicit `pending` items "
            "and check results describe gaps) or an `answer` (its `pending` "
            "array is empty; historical v1 saving required format checks and expert approval). "
            "Explicitly accepted scientific pending items remain in the saved "
            "provenance and approval in the v1 layout; an empty answer array does not mean all "
            "scientific questions have been resolved."
        ),
        "",
        (
            "Evidence keeps the expert's PDF discipline: a registered file with "
            "its content hash, a verbatim location description, an optional "
            "1-based PDF page ordinal (a position in the file, distinct from a "
            "printed page number), and an optional verbatim quote. Page "
            "ordinals are never guessed from the location text."
        ),
        "",
        "## Vocabulary",
        "",
        (
            f"Quantities reuse the {len(rules.quantity_paths)} approved science "
            f"rule paths at rules version `{rules.version}`; the author overall "
            "assessment states are the current rule states "
            f"({', '.join(f'`{state}`' for state in rules.state_ids)}). The "
            "legacy status vocabulary is preserved verbatim on migration via "
            "`legacy_status` "
            f"({', '.join(f'`{state}`' for state in LEGACY_AUTHOR_STATUSES)}); "
            "`not_assessed` no longer exists in the current rules and is "
            "reported for expert review instead of being converted."
        ),
        "",
        "| Vocabulary | Values |",
        "|---|---|",
        f"| limit kinds | {', '.join(f'`{v}`' for v in GOLD_LIMIT_KINDS)} |",
        f"| interval kinds | {', '.join(f'`{v}`' for v in GOLD_INTERVAL_KINDS)} |",
        f"| representations | {', '.join(f'`{v}`' for v in GOLD_REPRESENTATIONS)} |",
        f"| coordinate formats | {', '.join(f'`{v}`' for v in GOLD_COORDINATE_FORMATS)} |",
        "",
        "## Fields",
        "",
    ]
    for model in (GoldAnnotation,):
        lines += _model_table(model)
    for title, model in (
        ("Approval", SaveApproval),
        ("Maker identity", models_module.GoldActor),
        ("Human expert", models_module.GoldHuman),
        ("Original human expert", models_module.GoldOriginalExpert),
        ("Draft authorship", models_module.GoldPreparationAuthor),
        ("Material correspondence", models_module.GoldMaterialCorrespondence),
        ("LaTeX source", models_module.GoldLatexSource),
        ("Provenance", models_module.GoldProvenance),
        ("Legacy source", models_module.GoldLegacySource),
        ("Pending item", models_module.GoldPendingItem),
        ("Paper", models_module.GoldPaper),
        ("Rules reference", models_module.GoldRulesRef),
        ("PDF file", models_module.GoldPdfFile),
        ("Evidence", models_module.GoldEvidence),
        ("Contribution", models_module.GoldContribution),
        ("Identifier", models_module.GoldIdentifier),
        ("Boundness", models_module.GoldBoundness),
        ("Quantity group", models_module.GoldQuantityGroup),
        ("Value", models_module.GoldValue),
        ("Probability", models_module.GoldProbability),
        ("Error component", models_module.GoldErrorComponent),
        ("Conflict", models_module.GoldConflict),
        ("Exclusion", models_module.GoldExclusion),
    ):
        lines += [f"### {title}", ""]
        lines += _model_table(model)
    lines += [
        "## Legacy reader scope",
        "",
        (
            f"The reader accepts `{LEGACY_SCHEMA_NAME}` "
            f"v{LEGACY_SCHEMA_VERSION} only; older versions are rejected "
            "explicitly and never silently upgraded. Its vocabulary: "
            f"contribution types {', '.join(LEGACY_CONTRIBUTION_TYPES)}, limit "
            f"kinds {', '.join(LEGACY_LIMIT_KINDS)}, interval kinds "
            f"{', '.join(LEGACY_INTERVAL_KINDS)}, representations "
            f"{', '.join(LEGACY_REPRESENTATIONS)}, coordinate formats "
            f"{', '.join(LEGACY_COORDINATE_FORMATS)}."
        ),
        "",
    ]
    return "\n".join(lines)


def annotation_example() -> dict:
    """A complete, parseable synthetic draft; missing real metadata blocks saving."""
    rules = load_rules()
    ev = {"file_id": "pdf-1", "location": "Table 2, row SYN-A", "page": 3, "quote": ""}
    value = {
        key: None
        for key in (
            "value",
            "error",
            "lower_error",
            "upper_error",
            "unit",
            "range_lower",
            "range_upper",
            "coordinate_format",
            "condition",
            "interval_kind",
            "coverage",
            "lower_quantile",
            "upper_quantile",
            "representation",
            "source_note",
        )
    }
    value.update(
        value="612.0",
        error="8.0",
        unit="km/s",
        limit_kind="none",
        condition="Synthetic heliocentric measurement",
        source="this_paper",
        evidence=[ev],
        context_evidence=[],
        error_components=[],
    )
    return GoldAnnotation.model_validate(
        {
            "schema": {"name": GOLD_SCHEMA_NAME, "version": GOLD_SCHEMA_VERSION},
            "kind": "draft",
            "document_id": "gold-synthetic-example",
            "paper": {
                "paper_key": "synthetic-example",
                "arxiv_id": None,
                "version": None,
            },
            "rules": {"version": rules.version, "sha256": rules.sha256},
            "files": [{"file_id": "pdf-1", "name": "paper.pdf", "sha256": None}],
            "status": "contributions_found",
            "contributions": [
                {
                    "contribution_id": "c1",
                    "legacy_index": None,
                    "identifiers": [{"value": "SYN-A", "evidence": [ev]}],
                    "contribution_type": "candidates_found",
                    "contribution_summary": "Synthetic candidate report.",
                    "contribution_evidence": [ev],
                    "paper_boundness": {
                        "status": "unbound",
                        "legacy_status": None,
                        "evidence": [ev],
                    },
                    "quantities": [
                        {
                            "quantity": "observed_phase_space.radial_velocity",
                            "values": [value],
                        }
                    ],
                    "quantity_conflicts": [],
                }
            ],
            "reviewed_exclusions": [],
            "provenance": {"origin": "new_annotation", "prepared_by": None},
            "pending": [],
        }
    ).model_dump(mode="json", by_alias=True)


def render_annotation_template() -> str:
    rules = load_rules()
    quantity_list = "\n".join(f"- `{path}`" for path in rules.quantity_paths)
    skeleton = json.dumps(annotation_example(), indent=2, ensure_ascii=False)
    return (
        "<!-- Generated -->\n# Gold v1 migration working template\n\n"
        "This is a complete synthetic JSON example of a v1 working draft, not an approved answer. "
        "For current formal Gold, use the [two-file format](../../gold-format.md) "
        "and its templates in ../../templates/. "
        "Replace the example science with the actual paper. Null metadata is intentionally "
        "unresolved and blocks formal saving; never fabricate identities, dates or hashes. "
        "The companion gold-annotation.json contains the same parseable document.\n\n"
        f"## Instructions\n\n> {_ANNOTATION_INSTRUCTIONS}\n\n"
        "## Authorship and approval\n\n"
        "provenance.prepared_by records this draft's human or AI maker, model for AI, "
        "timezone-aware prepared_at, action and description. It does not authorize saving. "
        "For a migrated annotation, preserve legacy.annotator and legacy.annotated_at verbatim; "
        "record a confirmed original human separately in original_expert with the original "
        "record as basis. Unknown historical identity/time must remain unknown with a reason. "
        "Do not classify an old annotator as human by guessing.\n\n"
        "For a new working draft, original_expert may remain null. Before current-format "
        "conversion, explicitly identify its actual original annotator and reviewer; final "
        "approval is a separate human declaration for the concrete pair. No v1 formal save "
        "or provenance writer is provided. Revisions preserve original annotation identity. "
        "The original human expert may be will, shunhong_deng, or another explicitly identified "
        "expert; these names are not defaults. This records declared identities, not authentication.\n\n"
        "## Materials and technical fields\n\n"
        "Set paper.version explicitly. material_correspondence records the checked PDF file IDs, "
        "fixed LaTeX file/archive name, hash and version, human checker, checked_at, basis and "
        "known differences. Until correspondence is checked the draft cannot be saved formally. "
        "Hashes establish file identity, not scientific equivalence. Differences require explicit "
        "acceptance and later evaluation-scope review. PDF and LaTeX files are read from material_root.\n\n"
        "The JSON reader assigns omitted contribution_id, conflict_id, exclusion_id and item_id "
        "deterministically, preserving supplied IDs and rejecting duplicates. Supply document_id "
        "and registered file_id; record real file hashes. This draft is read-only to the "
        "formal writer; explicitly convert to the current pair before review/save. Git "
        "history, format versions, paper versions and rules versions are distinct.\n\n"
        f"## Quantity codes\n\n{quantity_list}\n\n## Synthetic draft\n\n```json\n{skeleton}\n```\n\n"
        "Missing evidence, conditions or hashes must be filled from known materials, not guessed. "
        "Draft pending categories are missing_info, structural_gap and science_review. Structural "
        "and material gaps block readiness. Conversion requires explicit pending dispositions; "
        "retained scientific uncertainty belongs in current answer.unresolved, not a separate "
        "formal provenance file. Conflicts preserve competing values and evidence.\n"
    )


def render_approval_template() -> str:
    return """<!-- Generated -->
# Human approval record

New answers and revisions use the same current-pair save. Organize the compact pair
before review; use check_save / gold inspect-save for a read-only binding summary.
This document is guidance, not approval. See the [Gold maintenance design](../../../../docs/design/evaluation.md).

Review answer.json, metadata.json and all reported checks. Set scientific_review=true
only after a human expert has reviewed this exact content. The binding hashes both
files (therefore their rules, material fingerprints and attribution), the absolute
target and expected old pair fingerprint. Save approval.json alongside the current
pair. Metadata review alone is not save authorization.
Use [the approval schema](../schemas/gold.approval.json) for the typed contract.
Old v4 inputs and v1 drafts remain read-only; explicitly convert them to a compact
pair before review. Old versioned targets and approvals are not adopted.

| Field | Required content |
|---|---|
| approved_by | Explicit human expert identifier |
| approver_kind | human (AI approval is rejected) |
| approved_at | Actual approval time, ISO 8601 with timezone |
| binding | Entire check_save binding, including target, files, rules, science_pending and expected_old |
| binding_sha256 | check_save binding.sha256; CLI inspect-save prints it |
| binding.expected_old | Current pair fingerprint from GoldTarget.fingerprint / gold read, or null for new Gold |
| basis | Human review and approval rationale |
| scientific_review | true only after human review of the exact pair |
| accepted_pending | Each explicitly accepted scientific pending ID |

Changed content, target or predecessor requires new approval. Revisions preserve
original annotation identity and record revision actors/date/basis and the previous
answer hash in metadata. Earlier approval is not refreshed to cover new content.
No identity provider or digital signature is implied. Tools never execute Git.
"""


def render_gold_index() -> str:
    return """<!-- Generated -->
# Generated Gold contracts

Generated from `hvs_catalog.evaluation` typed definitions by
`tools/render_record_contracts.py`; checked by its `--check`. Do not edit by hand.

## Current format

- [Gold format](gold-format.md) — formal answer and metadata fields
- [Answer schema](schemas/gold.answer.json) and [metadata schema](schemas/gold.metadata.json)
- [Answer template](templates/gold-answer.json) and [metadata template](templates/gold-metadata.json) — synthetic, unapproved pair
- [Approval schema](schemas/gold.approval.json) and [approval instructions](templates/gold-approval.md)

## Compatibility formats

The [legacy directory](legacy/gold-format.md) documents the migration working
format and the old Stella input reader. Use it when reading or migrating those
records. Current formal annotations use the files above.

- [Working annotation schema](legacy/schemas/gold.annotation.json)
- [Old Stella input schema](legacy/schemas/gold.legacy.json)
- [Working draft template](legacy/templates/gold-annotation.json) and [instructions](legacy/templates/gold-annotation.md)

## What the version numbers mean

Filenames identify each contract's purpose. Internal versions identify the format
that a reader must understand; they remain checked when files are read.

| Version | Meaning |
|---|---|
| Metadata `schema.version = 2` | Current two-file format; the earlier working annotation format used 1 |
| Old Stella `schema.version = 4` | A separate input format supported by the legacy reader |
| Git commit | Authorized historical content; no new vN directories or version index |
| Old saved `vN/` directories | Historical layout, rejected by the current writer; no migration performed |
| Paper or source `version` | The version of the paper or source material |
| Rules `version` | The scientific rule set used for the annotation |

These numbers advance independently. Moving contract files does not change a
saved answer, its metadata, its hashes or its Gold revision.

## Current save and manual recovery

Use one directory per paper, e.g. private-repo/data/2501.00001, not the repository
root. It contains gold-target.json, .lock, answer.json, metadata.json and approval.json.
Only new/empty or exactly compatible targets are accepted. A selected parent alias
(such as macOS /var) resolves to the real root, which the approval binds. The target
itself (including dangling symlinks) and internal managed paths must not be symlinks.
A parent alias redirected to a different root cannot reuse approval. Old layouts are
not read or migrated. The tool does not migrate existing private-repository data or
update repository documentation.

Python: organize drafts with compact_gold_annotation / write_compact_gold_pair;
then check_save(pair_path=..., material_root=..., target=GoldTarget(...),
expected_old=None or the current fingerprint). Have a human supply SaveApproval
with the returned binding and binding.sha256. Call approved_save with the same
pair/material/target and approval. Revisions use a separate work copy, preserve
original attribution, and include metadata.revision. GoldTarget.read_current()
checks the current approval; fingerprint() returns the pair digest. Plain format
parsers read drafts and do not certify saved targets.

CLI: gold inspect-save --pair WORK/answer.json --material-root MATERIALS --target
TARGET [--expected-old DIGEST] prints binding and binding_sha256 without writing.
After human approval, gold save --pair WORK/answer.json --material-root MATERIALS
--target TARGET --approval APPROVAL.json saves. gold read --target TARGET checks
current content and prints its fingerprint. Old prepare/query/git-refresh commands
and operation-id/preparation arguments are removed. No tool runs Git commands.

Interrupted save leaves .save-backup and refuses formal reads/writes. Stop writers,
hold the target .lock (SpaceLock), and preserve the entire incident directory before
manual recovery. The backup contains complete old/ and new/ triplets. Restore all
three files from one set, never mix them. For an empty old/ set (first save), remove
the three current files to restore the initialized empty target. Check restored
answer/metadata with read_compact_gold_pair, and validate approval.binding against
the authorized binding: hash the exact restored answer/metadata bytes, compare them
to approval.binding.answer_sha256/metadata_sha256, check the absolute target path,
validate the human approval fields and binding_sha256 (SaveBinding.sha256), and
check materials/pending via check_compact_gold_pair. Do not call inspect-save against
the interrupted target: it intentionally refuses.

After restoring and verifying that complete set (or the empty rollback), inspect
also the target root for atomic-write temporary leftovers: a process can stop after
writing .metadata.json.tmp-* (or an answer/approval temporary file), before replacing
the current file. These leftovers still block formal reads/writes, even when the
restored triplet is valid. Manually confirm each leftover's exact path and contents
against the preserved incident and the selected save's old/new files. A filename
pattern alone is not proof of ownership or authorization to delete. Preserve and
move each confirmed leftover to an incident-storage location outside the target,
without overwriting existing files. Do not use wildcard deletion, automatically
adopt leftovers, or clear other unknown files; if ownership cannot be established,
retain the blocked target for further manual investigation. Keep the complete
incident copy and moved residue as recovery evidence.

Only after this verification and manual move-out may .save-backup be removed;
then gold read must succeed (or report no answer for an empty rollback).
If cleanup was interrupted and backup sets are
incomplete, verify the complete current triplet against its approval before removing
the remnant. Unknown temporary siblings from interruption before backup publication
are not adopted or automatically cleaned. Preserve them for operator inspection.
The tool does not replay recovery or repair arbitrary damage. The backup covers the
previous uncommitted pair too; successful saves remove only this invocation's backup.
"""


def _schema_json(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(), indent=2, ensure_ascii=False) + "\n"


def _compact_example() -> tuple[CompactGoldAnswer, CompactGoldMetadata]:
    import hashlib

    rules = load_rules()
    evidence = {
        "file_id": "pdf-1",
        "location": "PDF p. 1, abstract",
        "page": 1,
        "quote": "Synthetic candidate SYN-A is reported.",
    }
    answer = CompactGoldAnswer.model_validate(
        {
            "arxiv_id": "0000.00000",
            "status": "contributions_found",
            "coverage": {"value": "complete"},
            "contributions": [
                {
                    "contribution_id": "0000.00000:cand01",
                    "identifiers": [{"value": "SYN-A", "evidence": [evidence]}],
                    "contribution_type": {
                        "value": "candidates_found",
                        "evidence": [evidence],
                    },
                    "contribution_summary": {
                        "value": "Synthetic candidate report.",
                        "evidence": [evidence],
                    },
                    "paper_boundness": {"value": "unbound", "evidence": [evidence]},
                    "quantities": [
                        {
                            "quantity": "observed_phase_space.radial_velocity",
                            "values": [
                                {
                                    "value": "612.0",
                                    "unit": "km/s",
                                    "error": "8.0",
                                    "condition": "Synthetic heliocentric measurement",
                                    "source": "this_paper",
                                    "evidence": [evidence],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )
    digest = hashlib.sha256(compact_answer_to_json(answer).encode("utf-8")).hexdigest()
    metadata = CompactGoldMetadata.model_validate(
        {
            "schema": {
                "name": COMPACT_METADATA_SCHEMA_NAME,
                "version": COMPACT_SCHEMA_VERSION,
            },
            "paper": {
                "arxiv_id": "0000.00000",
                "title": "Synthetic candidate paper",
                "version": "v1",
            },
            "rules": {"version": rules.version, "sha256": rules.sha256},
            "files": [{"file_id": "pdf-1", "name": "paper.pdf", "sha256": "0" * 64}],
            "source": {"name": "paper.tex", "sha256": "0" * 64, "version": "v1"},
            "annotation": {
                "original_annotator": "synthetic-expert",
                "reviewer": "synthetic-reviewer",
            },
            "review": {
                "material": {
                    "value": "checked",
                    "basis": "Synthetic example only; not a real material review",
                    "record_sha256": "0" * 64,
                },
                "scope": {
                    "value": "complete",
                    "basis": "Synthetic example only; not a real scope review",
                    "record_sha256": "0" * 64,
                },
                "answer_sha256": digest,
            },
        }
    )
    return answer, metadata


def render_compact_format_document() -> str:
    models = (CompactGoldAnswer, CompactGoldMetadata)
    lines = [
        "<!-- Generated -->",
        "# Formal Gold format",
        "",
        (
            "The formal scoring reference is `answer.json` together with `metadata.json`. "
            "The answer contains scientific targets, PDF evidence, explicit coverage and "
            "remaining uncertainty. Metadata binds that exact answer to the paper version, "
            "fixed PDF/source hashes, scientific rules and concise attribution. Optional "
            "empty fields are omitted; an empty `contributions` array remains explicit."
        ),
        "",
        (
            "`contribution_id` has the form `arxiv_id:candNN`; it is a generated "
            "identifier and needs no paper citation. Scientific claims use `value` and "
            "their corresponding `evidence`. The old v1 draft, migration indices, old "
            "boundness status, pending tasks and full historical provenance stay outside "
            "the formal pair."
        ),
        "",
        (
            "Material and scope confirmation in metadata is distinct from final Gold "
            "approval. New answers and revisions require `scientific_review=true` from a "
            "human approver bound to the exact pair, target and predecessor. The current "
            "`approval.json` is stored alongside the pair; no preparation or operation log is kept. "
            "Revisions record preparer, human decision author, date, basis and the previous "
            "answer hash in `revision`. Authorized Git commits retain history; uncommitted copies do not."
        ),
        "",
        (
            "See [version meanings](README.md#what-the-version-numbers-mean) for the distinction "
            "between format, paper, rules and saved Gold revisions."
        ),
        "",
        "## Top-level typed fields",
        "",
    ]
    for model in models:
        lines += _model_table(model)
    lines += ["## Nested typed fields", ""]
    for model in (
        CompactContribution,
        CompactIdentifier,
        CompactClaim,
        CompactBoundness,
        CompactQuantityGroup,
        CompactValue,
        CompactErrorComponent,
        CompactConflict,
        CompactExclusion,
        CompactCoverage,
        CompactScopeLimitation,
        CompactUnresolved,
        CompactPaper,
        CompactAnnotationActors,
        CompactReview,
        CompactReviewConfirmation,
        CompactRevision,
        models_module.GoldEvidence,
        models_module.GoldProbability,
        models_module.GoldRulesRef,
        models_module.GoldPdfFile,
        models_module.GoldLatexSource,
    ):
        lines += _model_table(model)
    return "\n".join(lines).rstrip() + "\n"


def build_outputs() -> dict[str, str]:
    compact_answer, compact_metadata = _compact_example()
    return {
        "README.md": render_gold_index(),
        "legacy/gold-format.md": render_gold_format_document(),
        "legacy/schemas/gold.annotation.json": _schema_json(GoldAnnotation),
        "legacy/schemas/gold.legacy.json": _schema_json(LegacyAnnotation),
        "legacy/templates/gold-annotation.md": render_annotation_template(),
        "legacy/templates/gold-annotation.json": json.dumps(
            annotation_example(), indent=2, ensure_ascii=False
        )
        + "\n",
        "templates/gold-approval.md": render_approval_template(),
        "schemas/gold.approval.json": _schema_json(SaveApproval),
        "gold-format.md": render_compact_format_document(),
        "schemas/gold.answer.json": _schema_json(CompactGoldAnswer),
        "schemas/gold.metadata.json": _schema_json(CompactGoldMetadata),
        "templates/gold-answer.json": compact_answer_to_json(compact_answer),
        "templates/gold-metadata.json": compact_metadata_to_json(compact_metadata),
    }


def write_outputs(root: Path) -> dict[str, str]:
    outputs = build_outputs()
    for relative, content in outputs.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return outputs


def check_outputs(root: Path) -> list[str]:
    """Return files differing from generated results; an empty list means synchronized."""
    expected = build_outputs()
    problems: list[str] = []
    for relative, content in expected.items():
        target = root / relative
        if not target.is_file():
            problems.append(f"缺失：gold/{relative}")
        elif target.read_text(encoding="utf-8") != content:
            problems.append(f"过期：gold/{relative}")
    for path in sorted(root.rglob("*")):
        if path.is_file():
            relative = str(path.relative_to(root))
            if relative not in expected:
                problems.append(f"多余：gold/{relative}")
    return problems
