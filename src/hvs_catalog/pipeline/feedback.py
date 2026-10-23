"""Present validator facts with one correction instruction for every submission."""

import json

from jsonschema import Draft202012Validator

from ..records.quantity_input import quantity_input_schema
from ..records.roster_input import roster_input_schema
from ..records.roster_validation import pointer, schema_issues, strict_json


def submission_schema(tool):
    return (
        quantity_input_schema()
        if tool == "submit_quantities"
        else roster_input_schema()
    )


def correction_instruction(tool):
    fields = " and ".join(submission_schema(tool)["properties"])
    return (
        "Correct the validation errors in the latest feedback.\n"
        f"Call {tool} once with the complete result.\n"
        f"Use {fields} as the only root fields.\n"
        "Follow the supplied schema.\n"
        "Preserve content unrelated to these errors."
    )


def schema_feedback(payload, schema):
    """Show schema constraints without guessing how fields should move."""
    errors, objects = [], {}
    for error in Draft202012Validator(schema).iter_errors(payload):
        path = pointer(error.absolute_path) if error.absolute_path else ""
        if error.validator in {"required", "additionalProperties"}:
            item = objects.setdefault(path, {"kind": "schema", "path": path})
            if error.validator == "required":
                item["missing"] = [
                    key for key in error.validator_value if key not in error.instance
                ]
            else:
                item["unexpected"] = sorted(
                    set(error.instance) - set(error.schema.get("properties", {}))
                )
        else:
            errors.append(
                {
                    "kind": "schema",
                    "path": path,
                    "rule": error.validator,
                    "constraint": error.validator_value,
                }
            )
    return [*objects.values(), *errors]


def submission_feedback(prior, issues, *, tool):
    """Keep syntax, schema, and protocol facts independent of specific mistakes."""
    calls = prior.get("tool_calls")
    protocol_ok = (
        isinstance(calls, list)
        and len(calls) == 1
        and isinstance(calls[0], dict)
        and calls[0].get("type", "function") == "function"
        and isinstance(calls[0].get("function"), dict)
        and calls[0]["function"].get("name") == tool
    )
    if not protocol_ok:
        return {
            "errors": [
                {
                    "kind": "protocol",
                    "expected_tool": tool,
                    "expected_calls": 1,
                    "received_calls": len(calls) if isinstance(calls, list) else 0,
                    "message": "The response does not contain exactly one call to the required tool.",
                }
            ]
        }
    try:
        arguments = calls[0]["function"]["arguments"]
        if not isinstance(arguments, str):
            raise TypeError("The submitted JSON source must be a string.")
        payload = strict_json(arguments)
    except json.JSONDecodeError as exc:
        return {
            "errors": [
                {
                    "kind": "json_syntax",
                    "message": exc.msg,
                    "line": exc.lineno,
                    "column": exc.colno,
                    "excerpt": arguments[max(0, exc.pos - 60) : exc.pos + 60],
                }
            ]
        }
    except (ValueError, TypeError, KeyError) as exc:
        return {"errors": [{"kind": "json_syntax", "message": str(exc)}]}
    schema = submission_schema(tool)
    errors = schema_feedback(payload, schema)
    schema_keys = {(e.path, e.message) for e in schema_issues(payload, schema)}
    for issue in issues:
        if (issue.path, issue.message) in schema_keys:
            continue
        if issue.path.startswith(("/tool_calls", "/content", "/response")):
            errors.append({"kind": "protocol", "message": issue.message})
        else:
            errors.append({"kind": "validation", **issue.model_dump()})
    unique = []
    for error in errors:
        if error not in unique:
            unique.append(error)
    return {"errors": unique}
