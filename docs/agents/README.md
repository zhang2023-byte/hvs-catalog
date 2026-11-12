# Agent skills

Configuration the engineering skills read. These files say where work is tracked and how
domain documentation is laid out; they do not add rules of their own, and where they touch
an area [Maintenance](../maintenance.md) owns, Maintenance wins.

| File | Role |
|---|---|
| [issue-tracker.md](issue-tracker.md) | GitHub Issues via `gh`, including wayfinding operations. |
| [domain.md](domain.md) | Single-context `GLOSSARY.md` and `docs/adr/`, and their scope. |

## Where a problem goes

The routing table lives in [Maintenance](../maintenance.md#documentation), together with the
rest of the documentation map, so there is only one copy of it. The rule it records, in one
line: a catalog defect is a GitHub issue labelled `data-defect`; other work is a GitHub issue
without it; rules live in Maintenance, and the reasoning behind a structural decision lives
in an ADR.
