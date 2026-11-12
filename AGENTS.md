# Agent entry

1. Read [Maintenance](docs/maintenance.md) for roles, authorization, verification, Git and
   documentation rules.
2. For literature processing, read [Pipeline](docs/pipeline.md); for catalog formats and
   commands, the [generated contracts](contracts/generated/README.md).
3. There is no progress log. Check the actual files, Git state and open GitHub issues before
   starting; read only the sections the task needs.

## Agent skills

### Issue tracker

All work and data problems live as GitHub issues in `zhang2023-byte/hvs-catalog`; a catalog
defect carries the `data-defect` label. See
[docs/agents/issue-tracker.md](docs/agents/issue-tracker.md).

### Triage labels

Each issue carries one category (`bug`, `enhancement`, or `data-defect`) and one state
(`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See
[docs/agents/triage-labels.md](docs/agents/triage-labels.md).

### Domain docs

Single-context: `GLOSSARY.md` and `docs/adr/` at the root, created lazily; ADRs record
architecture decisions and never override [Maintenance](docs/maintenance.md). See
[docs/agents/domain.md](docs/agents/domain.md).
