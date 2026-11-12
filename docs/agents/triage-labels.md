# Triage labels

The skills speak in terms of canonical triage roles. This file maps those roles to the label
strings used in this repo's issue tracker. When a skill mentions a role, use the corresponding
label string from these tables.

## Categories

Exactly one per issue. `data-defect` is this project's own category, a peer of the two defaults.

| Label in mattpocock/skills | Label in our tracker | Meaning |
|---|---|---|
| `bug` | `bug` | Something in the code is broken. |
| `enhancement` | `enhancement` | New feature or improvement. |
| (project category) | `data-defect` | A catalog value disagrees with the fixed PDF, or an identity is unresolved. The website's "Known issues" link filters on it, so readers see only these. |

## States

Exactly one per issue.

| Label in mattpocock/skills | Label in our tracker | Meaning |
|---|---|---|
| `needs-triage` | `needs-triage` | Maintainer needs to evaluate this issue. |
| `needs-info` | `needs-info` | Waiting on the reporter for more information. |
| `ready-for-agent` | `ready-for-agent` | Fully specified, ready for an AFK agent. For a `data-defect` this means the fix is mechanical: edit `catalog-review/*.json` and rebuild. |
| `ready-for-human` | `ready-for-human` | Needs the maintainer: paper reading or a scientific judgement. |
| `wontfix` | `wontfix` | Will not be actioned. |

State transitions: an unlabeled issue normally goes to `needs-triage` first; from there it moves
to `needs-info`, `ready-for-agent`, `ready-for-human`, or `wontfix`. `needs-info` returns to
`needs-triage` once the reporter replies. The maintainer can override at any time.

Where this touches an area [Maintenance](../maintenance.md) owns, Maintenance wins. In particular
a `data-defect` stays open until the next data version closes it, and issues are not a build
input.
