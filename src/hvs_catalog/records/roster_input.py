"""Compact model input, derived from the shared roster science definitions.

Only the wire shape differs: citations select supplied locations and contribution
fields sit directly on each entry. Binding supplies technical evidence metadata.
"""

from __future__ import annotations

import copy

from .submission import RosterSubmission

# Wire field -> position in the normalized contribution.
CONTRIBUTION_FIELDS = {
    "summary": ("summary", "text"),
    "summary_evidence": ("summary", "evidence"),
    "contribution_type": ("contribution_type", "value"),
    "type_evidence": ("contribution_type", "evidence"),
    "boundness": ("boundness", "status"),
    "boundness_evidence": ("boundness", "evidence"),
    "boundness_author_errors": ("boundness", "author_errors"),
    "boundness_gap_ids": ("boundness", "gap_ids"),
    "conditional_assessments": ("conditional_assessments",),
}


def roster_input_schema() -> dict:
    """Generate one public roster filling schema without duplicating science enums."""
    schema = RosterSubmission.model_json_schema()
    definitions = schema["$defs"]
    definitions["InlineEvidence"] = {
        "type": "object",
        "properties": {
            "location": {"type": "string", "minLength": 1},
            "quote": {"type": "string", "minLength": 1},
        },
        "required": ["location", "quote"],
        "additionalProperties": False,
    }
    entry = definitions["RosterEntrySubmission"]
    del entry["properties"]["contribution"]
    entry["properties"].pop("question")
    contribution = definitions["ContributionFieldsSubmission"]
    for name, path in CONTRIBUTION_FIELDS.items():
        field = contribution
        for key in path:
            if "$ref" in field:
                field = definitions[field["$ref"].rsplit("/", 1)[1]]
            field = field["properties"][key]
        entry["properties"][name] = copy.deepcopy(field)
    required = [
        "summary",
        "summary_evidence",
        "contribution_type",
        "type_evidence",
        "boundness",
        "conditional_assessments",
    ]
    entry["allOf"] = [
        {
            "if": {
                "properties": {"disposition": {"const": "included"}},
                "required": ["disposition"],
            },
            "then": {"required": required},
            "else": {
                "not": {"anyOf": [{"required": [key]} for key in CONTRIBUTION_FIELDS]}
            },
        }
    ]
    for predicate, field in (
        ({"disposition": {"enum": ["included", "excluded"]}}, "evidence"),
        (
            {
                "boundness": {
                    "enum": ["unbound", "possibly_unbound", "possibly_bound", "bound"]
                }
            },
            "boundness_evidence",
        ),
    ):
        entry["allOf"].append(
            {
                "if": {"properties": predicate, "required": list(predicate)},
                "then": {"required": [field], "properties": {field: {"minItems": 1}}},
            }
        )
    # Remove unreachable technical definitions from the model's reading burden.
    reachable = set()

    def visit(value):
        if isinstance(value, dict):
            if "$ref" in value:
                name = value["$ref"].rsplit("/", 1)[1]
                if name not in reachable:
                    reachable.add(name)
                    visit(definitions[name])
            for key, child in value.items():
                if key != "$defs":
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    schema["$defs"] = {
        name: value for name, value in definitions.items() if name in reachable
    }
    return schema


def bind_roster_input(payload: dict, locations: dict[str, dict]) -> dict:
    """Assemble the existing normalized contract without adding scientific claims."""

    def bind(value):
        if isinstance(value, dict):
            if set(value) == {"location", "quote"}:
                return {
                    "source": {
                        **copy.deepcopy(locations[value["location"]]),
                        "excerpt": value["quote"],
                    },
                    "parts": [],
                }
            return {key: bind(child) for key, child in value.items()}
        if isinstance(value, list):
            return [bind(child) for child in value]
        return value

    result = bind(payload)
    for entry in result["entries"]:
        contribution = {}
        for name, path in CONTRIBUTION_FIELDS.items():
            if name not in entry:
                continue
            target = contribution
            for key in path[:-1]:
                target = target.setdefault(key, {})
            target[path[-1]] = entry.pop(name)
        if contribution:
            entry["contribution"] = contribution
    return result
