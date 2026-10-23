"""Strict submission checks shared by acceptance, partial selection, and saving."""

from __future__ import annotations

from dataclasses import dataclass, field

from jsonschema import Draft202012Validator

from .evidence_audit import QuoteMismatch, quote_mismatches
from .response import (
    response_message,
    strict_json,
)
from .roster import RosterIssue
from .roster_input import RosterStar, roster_input_schema


def pointer(parts):
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def schema_issues(payload, schema=None):
    """Preserve stored rejection diagnostics for exact legacy snapshot replay.

    Model feedback renders constraints independently in pipeline.feedback.
    """
    for error in Draft202012Validator(schema or roster_input_schema()).iter_errors(
        payload
    ):
        path = pointer(error.absolute_path)
        if error.validator == "required":
            for name in error.validator_value:
                if name not in error.instance:
                    yield RosterIssue(
                        path=pointer((*error.absolute_path, name)),
                        message="Include this required field.",
                    )
            continue
        if error.validator == "additionalProperties":
            names = sorted(
                set(error.instance) - set(error.schema.get("properties", {}))
            )
            for name in names:
                yield RosterIssue(
                    path=pointer((*error.absolute_path, name)),
                    message="Remove this field. It is outside the schema.",
                )
            continue
        if (
            error.validator == "oneOf"
            and isinstance(error.instance, dict)
            and "value" in error.schema.get("properties", {})
            and "evidence" in error.schema.get("properties", {})
        ):
            message = "Use value, with optional errors, or use bounds without value or errors."
        elif error.validator == "not":
            message = "Do not combine error with lower_error or upper_error."
        elif error.validator == "dependentRequired":
            for flag, required in error.validator_value.items():
                if flag in error.instance:
                    for bound in required:
                        if bound not in error.instance:
                            yield RosterIssue(
                                path=pointer((*error.absolute_path, bound)),
                                message=f"Include {bound} when {flag} is present.",
                            )
            continue
        elif error.validator == "oneOf":
            location = (
                error.instance.get("location")
                if isinstance(error.instance, dict)
                else None
            )
            if isinstance(location, str) and location.startswith("I:"):
                message = "Image evidence must contain only location."
            elif isinstance(location, str) and location.startswith("T:"):
                message = (
                    "Text evidence must contain only location and a nonempty quote."
                )
            else:
                message = "Use text evidence with location and quote, or image evidence with location only."
        elif error.validator == "type":
            kind = error.validator_value
            article = "an" if kind[0] in "aeiou" else "a"
            message = f"Use {article} {kind}."
        elif error.validator == "minLength":
            message = "Use a nonempty string."
        elif error.validator == "minItems":
            message = "Include at least one item."
        elif error.validator == "enum":
            message = (
                "Use one of these values: " + ", ".join(error.validator_value) + "."
            )
        elif error.validator == "pattern":
            message = "Use a complete location such as T:p3-b5 or I:p5-b6."
        else:
            message = f"This field fails the schema rule: {error.validator}."
        yield RosterIssue(path=path, message=message)


def evidence_issues(payload, locations, *, quote_warnings=False):
    issues = []

    def walk(item, path):
        if isinstance(item, dict):
            if "location" in item:
                location = item["location"]
                bound = locations.get(location) if isinstance(location, str) else None
                if bound is None:
                    issues.append(
                        RosterIssue(
                            path=pointer((*path, "location")),
                            message=f"Location is not in the supplied material: {location!r}.",
                        )
                    )
                elif location.startswith("T:"):
                    quote = item.get("quote")
                    if (
                        not isinstance(quote, str)
                        or not quote
                        or (not quote_warnings and quote not in bound.get("text", ""))
                    ):
                        issues.append(
                            RosterIssue(
                                path=pointer((*path, "quote")),
                                message=f"Quote is not an unchanged excerpt at {location}.",
                            )
                        )
                elif not bound.get("images"):
                    issues.append(
                        RosterIssue(
                            path=pointer((*path, "location")),
                            message=f"No image was supplied at {location}.",
                        )
                    )
            for key, child in item.items():
                walk(child, (*path, key))
        elif isinstance(item, list):
            for index, child in enumerate(item):
                walk(child, (*path, index))

    walk(payload, ())
    return issues


def payload_issues(payload, locations, *, quote_warnings=False):
    """Return every structural, citation, and exact duplicate error without edits."""
    issues = list(schema_issues(payload))

    issues.extend(evidence_issues(payload, locations, quote_warnings=quote_warnings))

    stars = payload.get("stars") if isinstance(payload, dict) else None
    owners = {}
    if isinstance(stars, list):
        for i, star in enumerate(stars):
            identifiers = star.get("identifiers") if isinstance(star, dict) else None
            if not isinstance(identifiers, list):
                continue
            for identifier in identifiers:
                value = (
                    identifier.get("value") if isinstance(identifier, dict) else None
                )
                if isinstance(value, str) and value:
                    owners.setdefault(value, set()).add(i)
        for value, indices in sorted(owners.items()):
            if len(indices) > 1:
                for i in sorted(indices):
                    issues.append(
                        RosterIssue(
                            path=f"/stars/{i}/identifiers",
                            message=f"Star identifier {value!r} occurs in entries {sorted(indices)}.",
                        )
                    )
    return issues


@dataclass
class SubmissionCheck:
    payload: dict | None = None
    issues: list[RosterIssue] = field(default_factory=list)
    key: str = "stars"
    quote_warnings: bool = False
    warnings: list[QuoteMismatch] = field(default_factory=list)

    @property
    def readable(self):
        return isinstance(self.payload, dict) and isinstance(
            self.payload.get(self.key), list
        )

    @property
    def notes(self):
        value = self.payload.get("notes") if isinstance(self.payload, dict) else None
        return value if isinstance(value, str) else None

    @property
    def passed(self):
        return self.readable and not self.issues


def check_message(
    message,
    locations,
    *,
    tool="submit_roster",
    key="stars",
    validator=payload_issues,
    quote_warnings=False,
):
    calls = message.get("tool_calls") if isinstance(message, dict) else None
    if (
        not isinstance(calls, list)
        or len(calls) != 1
        or not isinstance(calls[0], dict)
        or not isinstance(calls[0].get("function"), dict)
        or calls[0]["function"].get("name") != tool
        or calls[0].get("type", "function") != "function"
    ):
        return SubmissionCheck(
            issues=[
                RosterIssue(path="/tool_calls", message=f"Call {tool} exactly once.")
            ]
        )
    try:
        arguments = calls[0]["function"]["arguments"]
        if not isinstance(arguments, str):
            raise TypeError("Tool arguments must be a JSON string")
        payload = strict_json(arguments)
    except (ValueError, TypeError, KeyError) as exc:
        return SubmissionCheck(
            issues=[RosterIssue(path="/arguments", message=str(exc))]
        )
    issues = validator(payload, locations, quote_warnings=quote_warnings)
    if message.get("content") not in (None, ""):
        issues.append(
            RosterIssue(
                path="/content", message="Do not return text outside the tool call."
            )
        )
    if not isinstance(calls[0].get("id"), str) or not calls[0]["id"]:
        issues.append(
            RosterIssue(
                path="/tool_calls/0/id",
                message="The tool call has no valid identifier.",
            )
        )
    return SubmissionCheck(
        payload if isinstance(payload, dict) else None,
        issues,
        key,
        quote_warnings,
        quote_mismatches(payload, locations) if quote_warnings else [],
    )


def select_partial(check: SubmissionCheck, locations):
    """Keep whole valid stars from one readable submission, in original order."""
    stars, indices, rejected = [], [], {}
    all_issues = payload_issues(
        check.payload, locations, quote_warnings=check.quote_warnings
    )
    for i, star in enumerate(check.payload["stars"]):
        issues = [
            e
            for e in all_issues
            if e.path == f"/stars/{i}" or e.path.startswith(f"/stars/{i}/")
        ]
        # Validate each entry independently even if the root failed first.
        entry = payload_issues(
            {"stars": [star], "notes": ""},
            locations,
            quote_warnings=check.quote_warnings,
        )
        for issue in entry:
            mapped = issue.model_copy(
                update={"path": issue.path.replace("/stars/0", f"/stars/{i}", 1)}
            )
            if mapped not in issues:
                issues.append(mapped)
        if issues:
            rejected[str(i)] = issues
        else:
            stars.append(RosterStar.model_validate(star))
            indices.append(i)
    notes = check.payload.get("notes")
    return stars, indices, rejected, notes if isinstance(notes, str) else None


def check_response(envelope, locations, **kwargs):
    check = check_message(
        response_message(envelope, effective=True), locations, **kwargs
    )
    error = envelope.get("error")
    if error and not check.issues:
        check.issues.append(
            RosterIssue(
                path="/response",
                message=f"The response failed protocol validation: {error}.",
            )
        )
    return check
