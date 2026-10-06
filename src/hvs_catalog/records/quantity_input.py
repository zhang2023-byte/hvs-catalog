"""PDF quantity citations without model-authored technical component identifiers."""

from __future__ import annotations

import copy

from .paper import COMPONENT_PARTS, TOP_LEVEL_PARTS
from .rules import quantity_paths
from .submission import QuantitySubmission, SubmissionError


def canonicalize_limit_operands(payload: dict) -> tuple[dict, list[dict]]:
    """Map an explicitly chosen one-sided limit's sole endpoint to its operand.

    Preserve every numerical string and the chosen scientific inequality. Do not
    guess when a central value or the opposite endpoint has also been supplied.
    """
    result = copy.deepcopy(payload)
    changes = []

    def walk(value, path):
        if isinstance(value, dict):
            kind = value.get("limit_kind")
            endpoint = (
                {"upper_limit": "range_upper", "lower_limit": "range_lower"}.get(kind)
                if isinstance(kind, str)
                else None
            )
            if (
                endpoint
                and value.get("value") is None
                and isinstance(value.get(endpoint), str)
            ):
                opposite = "range_lower" if endpoint == "range_upper" else "range_upper"
                if value.get(opposite) is None:
                    value["value"] = value.pop(endpoint)
                    changes.append(
                        {
                            "path": list(path),
                            "from_field": endpoint,
                            "to_field": "value",
                            "raw_value": value["value"],
                            "limit_kind": kind,
                        }
                    )
            for key, child in value.items():
                walk(child, (*path, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, (*path, index))

    walk(result, ())
    return result, changes


def quantity_input_schema() -> dict:
    """One fixed object, with flat scientific measurements and no repeated IDs."""
    schema = QuantitySubmission.model_json_schema()
    definitions = schema["$defs"]
    definitions["InlineEvidence"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "location": {"type": "string", "minLength": 1},
            "quote": {"type": "string", "minLength": 1},
        },
        "required": ["location", "quote"],
    }
    for name in (
        "QuantityGroupSubmission",
        "ConflictSubmission",
        "CoverageExceptionSubmission",
    ):
        definitions[name]["properties"]["quantity"]["enum"] = list(quantity_paths())
    definitions["ObjectQuestionSubmission"]["properties"]["quantity"]["enum"] = [
        *quantity_paths(),
        None,
    ]
    # In this PDF step the requested scope and supplied file are fixed. Inline
    # scientific questions already name their affected quantity; IDs are assigned
    # and linked by the converter, never authored by the model.
    for name, technical in (
        ("CoverageDeclarationBody", ("checked_file_ids", "gap_ids", "issue_ids")),
        ("CoverageExceptionSubmission", ("checked_file_ids", "gap_ids", "issue_ids")),
        ("CoverageDeclaration", ("scope",)),
    ):
        definition = definitions[name]
        for key in technical:
            definition["properties"].pop(key)
        definition["required"] = [
            k for k in definition["required"] if k not in technical
        ]
    question = definitions["ObjectQuestionSubmission"]
    question["properties"].pop("object_id")
    question["required"].remove("object_id")
    measurement = copy.deepcopy(definitions["ValueSubmission"])
    measurement["title"] = "MeasurementSubmission"
    measurement["properties"]["quantity"] = {
        "type": "string",
        "enum": list(quantity_paths()),
    }
    measurement["required"].append("quantity")
    definitions["MeasurementSubmission"] = measurement
    obj = definitions["ObjectQuantitySubmission"]["properties"]
    schema["title"] = "ObjectQuantityInput"
    schema["properties"] = {
        "measurements": {
            "type": "array",
            "items": {"$ref": "#/$defs/MeasurementSubmission"},
        },
        "quantity_conflicts": obj["quantity_conflicts"],
        "coverage": obj["coverage"],
        "precedent_uses": obj["precedent_uses"],
        "questions": schema["properties"]["questions"],
        "model_judgments": schema["properties"]["model_judgments"],
    }
    schema["required"] = ["measurements", "quantity_conflicts", "coverage", "questions"]
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
    schema["$defs"] = {k: v for k, v in definitions.items() if k in reachable}
    return schema


def expand_object_quantity_input(payload: dict, object_id: str) -> dict:
    """Bind the fixed target and group flat expressions without altering science.

    Invalid individual expressions remain present so ordinary validation and
    independent partial persistence can handle them, rather than dropping fields.
    """
    if not isinstance(payload.get("measurements"), list):
        raise SubmissionError("Measurements must be a list")
    groups = {}
    for entry in payload["measurements"]:
        value = copy.deepcopy(entry)
        quantity = value.pop("quantity", None) if isinstance(value, dict) else None
        key = repr(quantity)
        group = groups.setdefault(key, {"quantity": quantity, "values": []})
        group["values"].append(value)
    object_fields = {"measurements", "quantity_conflicts", "coverage", "precedent_uses"}
    result = {k: copy.deepcopy(v) for k, v in payload.items() if k not in object_fields}
    result["objects"] = [
        {
            "object_id": object_id,
            "quantities": list(groups.values()),
            "quantity_conflicts": copy.deepcopy(payload.get("quantity_conflicts", [])),
            "coverage": copy.deepcopy(payload.get("coverage", {})),
            "precedent_uses": copy.deepcopy(payload.get("precedent_uses", [])),
        }
    ]
    result["questions"] = []
    questions = payload.get("questions", [])
    if not isinstance(questions, list):
        result["questions"] = copy.deepcopy(questions)
        return result
    for question in questions:
        bound = copy.deepcopy(question)
        if isinstance(bound, dict):
            bound.setdefault("object_id", object_id)
        result["questions"].append(bound)
    return result


def bind_quantity_input(payload: dict, locations: dict[str, dict]) -> dict:
    """Associate explicitly filled numeric components with their supplied citations.

    Never normalize a number, infer its meaning, or add an absent component.
    Literal containment is not checked; scientific support remains unverified.
    Context support remains the extractor's responsibility; location validation
    subsequently checks the supplied text against the fixed PDF block.
    """

    def bind(value, parts=()):
        if isinstance(value, list):
            return [bind(child, parts) for child in value]
        if not isinstance(value, dict):
            return value
        if set(value) == {"location", "quote"}:
            if value["location"] not in locations:
                raise SubmissionError("Unknown supplied PDF citation location")
            return {
                "source": {
                    **copy.deepcopy(locations[value["location"]]),
                    "excerpt": value["quote"],
                },
                "parts": [
                    {"part": name, "raw_fragment": number} for name, number in parts
                ],
                "context": True,
            }
        if "limit_kind" in value:
            names = TOP_LEVEL_PARTS
        elif "name" in value and "evidence" in value:
            names = COMPONENT_PARTS
        elif "value" in value and "evidence" in value:
            names = ("value",)
        else:
            names = ()
        numeric = [(k, value[k]) for k in names if value.get(k) is not None]
        result = {
            k: bind(child, numeric if k == "evidence" else ())
            for k, child in value.items()
        }
        if numeric:
            covered = {
                p["part"] for e in result.get("evidence", []) for p in e["parts"]
            }
            missing = {k for k, _ in numeric} - covered
            if missing:
                raise SubmissionError(
                    "Filled numeric components require associated evidence: "
                    + ", ".join(sorted(missing))
                )
        return result

    result = bind(payload)
    supplied_files = sorted({location["file_id"] for location in locations.values()})
    for obj in result["objects"]:
        coverage = obj["coverage"]
        if "scope" in coverage:
            raise SubmissionError("Quantity scope is assigned by the application")
        coverage["scope"] = "all_requested"
        for declaration in [coverage["common"], *coverage["exceptions"]]:
            if any(
                k in declaration for k in ("checked_file_ids", "gap_ids", "issue_ids")
            ):
                raise SubmissionError(
                    "Quantity coverage references are assigned by the application"
                )
            declaration.update(
                checked_file_ids=[]
                if declaration["state"] == "not_checked"
                else supplied_files,
                gap_ids=[],
                issue_ids=[],
            )
    return result
