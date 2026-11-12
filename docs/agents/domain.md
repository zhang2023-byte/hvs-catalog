# Domain docs

How the engineering skills read this repo's domain documentation.

## Before exploring, read these

- `GLOSSARY.md` at the repo root, or `GLOSSARY-MAP.md` if it exists, which points at one
  `GLOSSARY.md` per context.
- `docs/adr/`: the decisions that touch the area you are about to change.

Missing files are not an error. `/domain-modeling`, reached through `/grill-with-docs` and
`/improve-codebase-architecture`, creates them when a term or a decision is actually
resolved.

## Layout

Single-context repo: one `GLOSSARY.md` and `docs/adr/` at the root.

## Vocabulary

Name domain concepts with the terms `GLOSSARY.md` defines. If a concept has no term yet,
either it is language the project does not use or it is a real gap to note for
`/domain-modeling`.

## ADR scope

ADRs record architecture and interface decisions: module seams, dependency direction,
storage and error contracts. They do not restate or override
[Maintenance](../maintenance.md), which owns data-version, science and Git rules. Where an
idea conflicts with an ADR, say so explicitly instead of overriding it silently.

The split, in one line: a rule that decides **what the data are and how a version is built**
belongs in Maintenance; a rule about **how the code is divided, where an interface sits or
which dependency direction is allowed** belongs in an ADR. If a change alters both, it needs
both. Write an ADR only when the decision is hard to reverse, surprising later, and the
result of a real trade-off; otherwise record it in the code or in Git history.
