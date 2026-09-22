<!-- Generated -->
# Generated data contracts

Shared record contracts are generated from `hvs_catalog.records`.
The [Gold contracts](gold/README.md) in `gold/` are generated from
`hvs_catalog.evaluation`. Regenerate both with
`uv run python tools/render_record_contracts.py` and check with
`--check`. Do not edit by hand.

- `record-format.md` — full record format documentation
- `schemas/submission.roster.json`, `schemas/submission.candidate.json`,
  `schemas/submission.quantity.json` — step filling formats
- `templates/roster-extraction.md`, `templates/candidate-judgment.md`,
  `templates/quantity-extraction.md` — English step filling templates
  sent to extractors

Per record type:

- `schemas/record.decision.json` — `decision` records
- `schemas/record.evidence.json` — `evidence` records
- `schemas/record.external_record.json` — `external_record` records
- `schemas/record.object.json` — `object` records
- `schemas/record.paper.json` — `paper` records
- `schemas/record.question.json` — `question` records
- `schemas/record.release.json` — `release` records
- `schemas/record.roster.json` — `roster` records
- `schemas/record.run.json` — `run` records
- `schemas/record.source_package.json` — `source_package` records
