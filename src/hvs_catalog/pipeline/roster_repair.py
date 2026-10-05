"""Small, application-selected replacements for an otherwise immutable submission."""

from __future__ import annotations

import copy
from dataclasses import dataclass

from jsonschema import Draft202012Validator

from ..llm.config import Tool


@dataclass
class Repair:
    path: tuple
    schema: dict
    reason: str
    operation: str = "replace"


def at(value, path):
    for key in path:
        value = value[key]
    return value


def schema_repairs(payload: dict, schema: dict) -> list[Repair]:
    """Expose only invalid or absent fields, never a valid parent object."""
    fixes = []

    def walk(value, spec, path):
        if "$ref" in spec:
            spec = schema["$defs"][spec["$ref"].rsplit("/", 1)[1]]
        if "anyOf" in spec:
            if value is None and any(s.get("type") == "null" for s in spec["anyOf"]):
                return
            non_null = [s for s in spec["anyOf"] if s.get("type") != "null"]
            if len(non_null) == 1:
                return walk(value, non_null[0], path)
        if isinstance(value, dict) and spec.get("type") == "object":
            properties = copy.deepcopy(spec.get("properties", {}))
            required = set(spec.get("required", []))
            for branch in spec.get("allOf", []):
                if Draft202012Validator(branch["if"]).is_valid(value):
                    required.update(branch["then"].get("required", []))
                    for key, constraints in (
                        branch["then"].get("properties", {}).items()
                    ):
                        properties[key].update(constraints)
                elif "else" in branch and value.get("disposition") in {
                    "excluded",
                    "pending",
                }:
                    for item in branch["else"]["not"]["anyOf"]:
                        key = item["required"][0]
                        if key in value:
                            fixes.append(
                                Repair(
                                    (*path, key),
                                    {"type": "null"},
                                    "Remove a field inapplicable to this disposition; submit null to remove it.",
                                    "delete",
                                )
                            )
            for key in sorted(required - value.keys()):
                fixes.append(
                    Repair(
                        (*path, key),
                        properties[key],
                        "Supply this missing required field.",
                    )
                )
            for key, child in value.items():
                if key not in properties:
                    fixes.append(
                        Repair(
                            (*path, key),
                            {"type": "null"},
                            "Remove this unknown field; submit null to remove it.",
                            "delete",
                        )
                    )
                else:
                    walk(child, properties[key], (*path, key))
        elif (
            isinstance(value, list)
            and spec.get("type") == "array"
            and len(value) >= spec.get("minItems", 0)
        ):
            for index, child in enumerate(value):
                walk(child, spec["items"], (*path, index))
        elif not Draft202012Validator({**spec, "$defs": schema["$defs"]}).is_valid(
            value
        ):
            fixes.append(
                Repair(
                    path,
                    spec,
                    "Correct the type, allowed value, or required nonempty content of this field.",
                )
            )

    walk(payload, schema, ())
    # Prefer the explicitly selected parent if several diagnostics overlap.
    unique = {fix.path: fix for fix in fixes}
    return [
        fix
        for path, fix in unique.items()
        if not any(path[:i] in unique for i in range(len(path)))
    ]


def repair_tool(fixes: list[Repair], schema: dict) -> Tool:
    return Tool(
        name="repair_roster",
        description="Submit only the requested replacements. All other fields remain unchanged in the application.",
        parameters={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                f"R{i}": {**fix.schema, "description": fix.reason}
                for i, fix in enumerate(fixes, 1)
            },
            "required": [f"R{i}" for i in range(1, len(fixes) + 1)],
            "$defs": schema["$defs"],
        },
    )


def apply_repairs(payload: dict, fixes: list[Repair], values: dict) -> dict:
    """Apply an all-or-nothing patch whose paths were chosen by the application."""
    if set(values) != {f"R{i}" for i in range(1, len(fixes) + 1)}:
        raise ValueError(
            "Repair response must contain exactly the requested repair IDs"
        )
    result = copy.deepcopy(payload)
    for index, fix in enumerate(fixes, 1):
        target = at(result, fix.path[:-1])
        if fix.operation == "delete":
            del target[fix.path[-1]]
        elif fix.operation == "append":
            target[fix.path[-1]].append(copy.deepcopy(values[f"R{index}"]))
        else:
            target[fix.path[-1]] = copy.deepcopy(values[f"R{index}"])
    return result
