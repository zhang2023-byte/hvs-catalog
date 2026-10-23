"""Generate only current contracts from their shared definitions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .extraction_input import ExtractionInput
from .extraction_run import ExtractionRun
from .literature_contribution import (
    ImageEvidence,
    LiteratureContribution,
    LiteratureContributionType,
    LiteratureIdentifier,
    LiteratureMetadata,
    LiteratureQuantity,
    LiteratureStar,
    LiteratureValue,
    TextEvidence,
)
from .registry import RECORD_MODELS

_FOREIGN_SUBTREES = frozenset({"gold", "scoring"})


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


def render_literature_contribution_format() -> str:
    lines = [
        "<!-- Generated from hvs_catalog.records.literature_contribution. -->",
        "# Literature contribution format",
        "",
        "`literature_contribution.json` contains one paper's scientific contribution.",
        "Its root contains only `metadata`, `stars`, and `notes`. It has no record",
        "envelope or runtime information. The program assembles this file from stage snapshots.",
        "The separate `run.json` file records execution status and archive references.",
        "",
        "Schema: [literature_contribution.json](schemas/literature_contribution.json).",
        "Complete fictional example: [literature_contribution.json](../../tests/fixtures/literature_contribution.json).",
        "",
        "## Notes",
        "",
        "The final `notes` field is an array of nonempty strings.",
        "Put the roster note first, then quantity notes in saved roster order.",
        "Prefix each quantity note with `identifiers[0].value` for its star and `: `.",
        "Omit empty notes. Use `[]` when no note is necessary.",
        "Each extraction stage still submits its own `notes` as a string.",
        "The final assembly step must add the prefixes and preserve this order.",
        "",
        "## Numerical forms and evidence",
        "",
        "- Point value: `value`, optionally `error` or `lower_error` / `upper_error`.",
        "  Symmetric and asymmetric errors cannot coexist. A reported one-sided error",
        "  may stand alone; the other side is not filled in.",
        "- Upper or lower limit: only `upper_bound` or `lower_bound`.",
        "- Pure interval: both `lower_bound` and `upper_bound`, with no point value",
        "  or error fields. Do not calculate a midpoint or infer an error.",
        "- Optional `lower_inclusive` / `upper_inclusive` requires the matching bound.",
        "  Use a boolean only when endpoint inclusion is reported. Otherwise omit it.",
        "- Numerical components remain original strings, preserving signs, precision,",
        "  trailing zeros, coordinate notation, and unevaluated expressions.",
        "- Omit unreported optional fields; do not fill them with `null` or defaults.",
        "  `unit` and `condition` are optional. Put reference frames and relevant",
        "  statistical meanings or confidence levels in `condition`.",
        "- Each identifier, contribution type, and value has a nonempty `evidence`",
        "  array. Text evidence contains `location` and an unchanged `quote`.",
        "  Image evidence contains only `location`. Captions use text locations.",
        "",
        "## Validation and JSON access",
        "",
        "Unknown fields and coercion of numbers to strings or integers to booleans",
        "are rejected. Quantity identifiers reuse the existing science definitions",
        "without changing their meaning. Structural validation does not authenticate",
        "quotes or locations, decide scientific inclusion, resolve conflicts,",
        "compare interval endpoints, or verify any physical value.",
        "",
        "The public Python API is available from `hvs_catalog.records`:",
        "",
        "```python",
        "from hvs_catalog.records import (",
        "    LiteratureContribution,",
        "    literature_contribution_to_json,",
        "    load_literature_contribution,",
        "    parse_literature_contribution,",
        "    save_literature_contribution,",
        ")",
        "",
        'document = load_literature_contribution("literature_contribution.json")',
        "text = literature_contribution_to_json(document)",
        "validated = parse_literature_contribution(text)",
        'save_literature_contribution("output/literature_contribution.json", validated)',
        "schema = LiteratureContribution.model_json_schema()",
        "```",
        "",
        "Parsing accepts a JSON string or a dictionary and raises a Pydantic",
        "`ValidationError` for invalid structure or JSON. Saving validates before",
        "writing UTF-8 JSON, creates parent directories, and keeps `notes` last.",
        "File I/O errors propagate to the caller. This API does not add a transaction",
        "or connect to workspace persistence.",
        "",
        "## Fields",
        "",
    ]
    for model in (
        LiteratureContribution,
        LiteratureMetadata,
        LiteratureStar,
        LiteratureIdentifier,
        LiteratureContributionType,
        LiteratureQuantity,
        LiteratureValue,
        TextEvidence,
        ImageEvidence,
    ):
        lines.extend(_model_table(model))
    return "\n".join(lines)


def _schema_json(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(), indent=2, ensure_ascii=False) + "\n"


def write_outputs(root: Path) -> dict[str, str]:
    outputs = build_outputs()
    for relative, content in outputs.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return outputs


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


def run_example():
    """An unstarted fictional execution has no selected rounds or scientific result."""
    ref = {"path": "runs/inputs/synthetic/input.json", "sha256": "0" * 64}
    return ExtractionRun.model_validate(
        {
            "execution_id": "synthetic-example",
            "scope": "full",
            "status": {"execution": "created", "result": None, "resumable": True},
            "timestamps": {"started_at": "2026-01-01T00:00:00Z", "finished_at": None},
            "input_ref": ref,
            "stages": {
                "roster": {
                    "state": "created",
                    "status": None,
                    "snapshot_ref": None,
                    "source_execution": None,
                    "rounds_completed": 0,
                    "source_round": None,
                    "fallback": None,
                    "notes_available": None,
                },
                "quantities": [],
            },
            "usage": {"attempts": 0, "known_tokens": 0, "total_tokens": 0},
            "errors": [],
            "trace_ref": {
                "path": "runs/synthetic-example/traces/empty.json",
                "sha256": "0" * 64,
            },
            "result_ref": None,
        }
    )


def build_outputs():
    from ..llm.prompts import assemble_blueprint

    outputs = {
        "README.md": (
            "# Current contracts\n\n"
            "The approved prompts and tool definitions are generated from the project sources.\n\n"
            "| Stage | System prompt | Tool definition | Submission schema | Execution |\n"
            "|---|---|---|---|---|\n"
            "| Roster | [System](prompts/roster.md) | [Tool](tools/submit_roster.json) | [Schema](schemas/submission.roster.json) | Available |\n"
            "| Quantities | [System](prompts/quantities.md) | [Tool](tools/submit_quantities.json) | [Schema](schemas/submission.quantities.json) | Available |\n\n"
            "Both stages use the same frozen paper material. Quantity requests add a second user message with target identifiers.\n\n"
            "Text-only inputs use separate [roster](prompts/roster-text-only.md) and "
            "[quantity](prompts/quantities-text-only.md) material instructions. "
            "They retain parser text and omit images from requests. Scientific rules and submission schemas remain the same.\n\n"
            "The final scientific result uses [literature_contribution.json](schemas/literature_contribution.json). "
            "The [run summary](schemas/extraction_run.json) records status and exact references. "
            "Its [fictional example](examples/run.json) uses placeholder references. "
            "The [input schema](schemas/extraction_input.json) defines the frozen input. "
            "The [scoring contract](scoring/README.md) defines fixed references, manifests and offline replay. "
            "[Gold tools](gold/README.md) use original-PDF evidence and current scientific fields.\n"
        ),
        "literature-contribution-format.md": render_literature_contribution_format(),
        "schemas/literature_contribution.json": _schema_json(LiteratureContribution),
        "schemas/extraction_run.json": _schema_json(ExtractionRun),
        "schemas/extraction_input.json": _schema_json(ExtractionInput),
        "examples/run.json": run_example().model_dump_json(indent=2) + "\n",
    }
    for stage in ("roster", "quantities"):
        blueprint = assemble_blueprint(stage)
        outputs[f"schemas/submission.{stage}.json"] = (
            json.dumps(blueprint.schema_, ensure_ascii=False, indent=2) + "\n"
        )
        outputs[f"prompts/{stage}.md"] = (
            "<!-- Generated -->\n" + blueprint.system + "\n"
        )
        outputs[f"prompts/{stage}-text-only.md"] = (
            "<!-- Generated -->\n"
            + assemble_blueprint(stage, "text_only").system
            + "\n"
        )
        outputs[f"tools/submit_{stage}.json"] = (
            json.dumps(
                {"type": "function", "function": blueprint.tool().model_dump()},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    for name, model in RECORD_MODELS.items():
        outputs[f"schemas/record.{name}.json"] = _schema_json(model)
    return outputs
