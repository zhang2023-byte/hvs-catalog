<!-- Generated -->
# Generated Gold contracts

Generated from `hvs_catalog.evaluation` typed definitions by
`tools/render_record_contracts.py`; checked by its `--check`. Do not edit by hand.

## Current format

- [Gold format](gold-format.md) — formal answer and metadata fields
- [Answer schema](schemas/gold.answer.json) and [metadata schema](schemas/gold.metadata.json)
- [Answer template](templates/gold-answer.json) and [metadata template](templates/gold-metadata.json) — synthetic, unapproved pair
- [Approval schema](schemas/gold.approval.json) and [approval instructions](templates/gold-approval.md)

## Compatibility formats

The [legacy directory](legacy/gold-format.md) documents the migration working
format and the old Stella input reader. Use it when reading or migrating those
records. Current formal annotations use the files above.

- [Working annotation schema](legacy/schemas/gold.annotation.json)
- [Old Stella input schema](legacy/schemas/gold.legacy.json)
- [Working draft template](legacy/templates/gold-annotation.json) and [instructions](legacy/templates/gold-annotation.md)

## What the version numbers mean

Filenames identify each contract's purpose. Internal versions identify the format
that a reader must understand; they remain checked when files are read.

| Version | Meaning |
|---|---|
| Metadata `schema.version = 2` | Current two-file format; the earlier working annotation format used 1 |
| Old Stella `schema.version = 4` | A separate input format supported by the legacy reader |
| Saved directory `v1/`, `v2/`, ... | Successive Gold revisions for one paper |
| Paper or source `version` | The version of the paper or source material |
| Rules `version` | The scientific rule set used for the annotation |

These numbers advance independently. Moving contract files does not change a
saved answer, its metadata, its hashes or its Gold revision.
