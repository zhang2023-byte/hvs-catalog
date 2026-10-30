<!-- Generated -->
# Annotation format

Each `data/<arxiv_id>/` contains one fixed `paper.pdf` and one `annotation.json`.
For older identifiers containing `/`, replace that separator with `_` in the directory
name only. Keep the original identifier in metadata. PDF files are ignored by Git.

The annotation has `metadata`, `stars` and `notes`. Metadata contains the paper's
identifier, version, title and authors; `format: "hvs.gold-annotation"`;
`format_version: 1`; science `rules.version` and `rules.sha256`; and
`material.path: "paper.pdf"` with its exact byte `sha256`. The paper version is separate
from the annotation format version. Hashes identify content; they do not certify truth.

Scientific fields and numerical validation are shared with extraction results.
Every identifier, contribution type and numerical value has PDF evidence containing
only `page` and `location`. Page means the one-based PDF file page ordinal, not the
printed page label. Location names the visible table, row, section or passage.
All evidence refers to the single adjacent PDF. There are no `file_id` or `quote`
fields, separate metadata files, approval records, assistant identities or parser
archive bindings. Write descriptive fields in English. Preserve scientific strings,
printed precision, uncertainty forms and units; do not compute replacement values.

Only human-confirmed complete answers belong in `data/`. Drafts, unresolved questions,
comparison reports and migration backups belong in `.work/`. Schema and PDF checks
cannot establish completeness, scientific correctness or human confirmation. These
are publication prerequisites, not self-reported flags inside the annotation.
Historical formats are not converted silently.
