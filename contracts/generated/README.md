# Current contracts

The approved prompts and tool definitions are generated from the project sources.

| Stage | System prompt | Tool definition | Submission schema | Execution |
|---|---|---|---|---|
| Roster | [System](prompts/roster.md) | [Tool](tools/submit_roster.json) | [Schema](schemas/submission.roster.json) | Available |
| Quantities | [System](prompts/quantities.md) | [Tool](tools/submit_quantities.json) | [Schema](schemas/submission.quantities.json) | Available |

Both stages use the same frozen paper material. Quantity requests add a second user message with target identifiers.

Text-only inputs use separate [roster](prompts/roster-text-only.md) and [quantity](prompts/quantities-text-only.md) material instructions. They retain parser text and omit images from requests. Scientific rules and submission schemas remain the same.

The final scientific result uses [literature_contribution.json](schemas/literature_contribution.json). The [run summary](schemas/extraction_run.json) records status and exact references. The [evidence audit](schemas/evidence_audit.json) binds quote warnings to the exact scientific result. Its [fictional example](examples/run.json) uses placeholder references. The [input schema](schemas/extraction_input.json) defines the frozen input. The [scoring contract](scoring/README.md) defines fixed references, manifests and offline replay. [Gold tools](gold/README.md) use original-PDF evidence and current scientific fields.
