"""Check numerical submissions and retain whole values from one response."""

from .literature_contribution import LiteratureQuantity
from .quantity_input import quantity_input_schema
from .roster import RosterIssue
from .roster_validation import (
    check_response as check_submission,
)
from .roster_validation import (
    evidence_issues,
    schema_issues,
)


def payload_issues(payload, locations):
    issues = list(schema_issues(payload, quantity_input_schema()))
    issues.extend(evidence_issues(payload, locations))
    groups = payload.get("quantities") if isinstance(payload, dict) else None
    owners = {}
    if isinstance(groups, list):
        for index, group in enumerate(groups):
            quantity = group.get("quantity") if isinstance(group, dict) else None
            if isinstance(quantity, str):
                owners.setdefault(quantity, []).append(index)
        for quantity, indices in sorted(owners.items()):
            if len(indices) > 1:
                for index in indices:
                    issues.append(
                        RosterIssue(
                            path=f"/quantities/{index}/quantity",
                            message=f"Quantity {quantity!r} occurs in groups {indices}. Submit one group for this quantity.",
                        )
                    )
    return issues


def check_response(envelope, locations):
    return check_submission(
        envelope,
        locations,
        tool="submit_quantities",
        key="quantities",
        validator=payload_issues,
    )


def select_partial(check, locations):
    quantities, paths, rejected = [], [], {}
    issues = payload_issues(check.payload, locations)
    for index, group in enumerate(check.payload["quantities"]):
        path = f"/quantities/{index}"
        group_issues = [
            e
            for e in issues
            if (e.path == path or e.path.startswith(path + "/"))
            and not e.path.startswith(path + "/values/")
        ]
        if group_issues:
            rejected[path] = group_issues
            continue
        values = []
        for offset, value in enumerate(group["values"]):
            value_path = f"{path}/values/{offset}"
            errors = [
                e
                for e in issues
                if e.path == value_path or e.path.startswith(value_path + "/")
            ]
            if errors:
                rejected[value_path] = errors
            else:
                values.append(value)
                paths.append(value_path)
        if values:
            quantities.append(
                LiteratureQuantity.model_validate(
                    {"quantity": group["quantity"], "values": values}
                )
            )
    notes = check.payload.get("notes")
    return quantities, paths, rejected, notes if isinstance(notes, str) else None
