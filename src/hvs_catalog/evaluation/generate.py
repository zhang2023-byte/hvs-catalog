"""从 Gold 有类型定义生成格式说明、JSON Schema 与标注模板。

生成内容写入 ``contracts/generated/gold/``，由
``tools/render_record_contracts.py`` 一并渲染与检查；生成文件不手工修
改。旧 v4 读取适配器的 JSON Schema 一并生成，便于对照迁移输入范围。
"""

from __future__ import annotations

import json
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
    doc = (model.__doc__ or "").strip()
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
        "For a new annotation, original_expert stays null: the first human approval is the "
        "authority for original authorship. Saved answers carry original_approval_version=1 "
        "and provenance.json points to that version's approval.json. Later approvals never "
        "replace the original expert. Each revision records its own maker and human approval. "
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
        "and registered file_id; record real file hashes. Gold revision numbers are assigned by "
        "the save tool and are independent of paper versions.\n\n"
        f"## Quantity codes\n\n{quantity_list}\n\n## Synthetic draft\n\n```json\n{skeleton}\n```\n\n"
        "Missing evidence, conditions or hashes must be filled from known materials, not guessed. "
        "Draft pending categories are missing_info, structural_gap and science_review. Structural "
        "and material gaps block saving. Explicitly accepted scientific pending items retain their "
        "complete descriptions in the formal provenance. Conflicts preserve competing values and evidence.\n"
    )


def render_approval_template() -> str:
    return """<!-- Generated -->
# Human approval record

This template applies to immutable version saves. An authorized local correction
edits the existing answer and metadata in place and records the existing human decision;
it does not need a new preparation approval. See the [Gold maintenance design](../../../../docs/design/evaluation.md).

For the current two-file Gold format, review both answer.json and metadata.json, plus the check report.
Set scientific_review=true only after a human expert has reviewed the scientific
content of that exact pair. The approved manifest hashes both files, fixed materials,
rules and target. The approval is saved as a protected operation sidecar; the formal
version directory contains answer.json and metadata.json only.

For a historical v1 save, the prepared package included answer and provenance.
After reviewing the prepared answer, provenance and check report, a human expert supplies
an approval matching that preparation. This document is guidance, not an approval.
Use [the approval schema](../schemas/gold.approval.json) for the full typed contract.

| Field | Required content |
|---|---|
| approved_by | Explicit human expert identifier |
| approver_kind | human (AI approval is rejected) |
| approved_at | Actual approval time, ISO 8601 with timezone |
| document_id, target_path | Exact document and target from the manifest |
| manifest_sha256 | Hash of the exact reviewed preparation |
| expected_old | Exact predecessor reference, or null for first save |
| rules_version, rules_sha256 | Exact rules reference from the manifest |
| basis | Human review and approval rationale |
| scientific_review | true for a current two-file save after reviewing the exact answer and metadata; false otherwise |
| accepted_pending | Each explicitly accepted scientific pending ID |

Any change to the answer, maker, original attribution, materials or target requires a new
preparation and approval. Historical v1 keeps approval.json in its version directory;
v2 keeps the approval in operations. Never replace an old approval with a later
expert's name. No identity provider or digital signature is implied.
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
| Saved directory `v1/`, `v2/`, ... | Original save location; local corrections edit these files in place and Git retains history |
| Paper or source `version` | The version of the paper or source material |
| Rules `version` | The scientific rule set used for the annotation |

These numbers advance independently. Moving contract files does not change a
saved answer, its metadata, its hashes or its Gold revision.
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
            "approval. Immutable version save additionally requires `scientific_review=true` from a "
            "human approver for the exact answer and metadata manifest. The operation "
            "record retains that approval separately from the two-file package. "
            "In-place local corrections record their preparer, human decision author and "
            "basis in `revision`; earlier approvals apply only to the original content preserved in Git history."
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
    """返回与生成结果不一致的文件列表；空列表表示同步。"""
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
