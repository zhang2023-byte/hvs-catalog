"""Generate the single-file Gold contract."""

import json
from pathlib import Path

from ..records.pdf_contribution import GoldAnnotation
from ..records.rules import current_rules_ref


def example_annotation():
    evidence = [{"page": 1, "location": "Table 1, row FIC-A"}]
    return GoldAnnotation.model_validate(
        {
            "metadata": {
                "arxiv_id": "2601.99999",
                "version": "v1",
                "title": "A fictional annotation example",
                "authors": ["A. Example"],
                "rules": current_rules_ref(),
                "material": {"path": "paper.pdf", "sha256": "0" * 64},
            },
            "stars": [
                {
                    "identifiers": [{"value": "FIC-A", "evidence": evidence}],
                    "contribution_type": {
                        "value": "candidates_found",
                        "evidence": evidence,
                    },
                    "quantities": [
                        {
                            "quantity": "radial_velocity",
                            "values": [
                                {
                                    "value": "520",
                                    "error": "10",
                                    "unit": "km/s",
                                    "evidence": evidence,
                                }
                            ],
                        }
                    ],
                }
            ],
            "notes": [],
        }
    )


def build_gold_outputs():
    return {
        "schemas/annotation.json": json.dumps(
            GoldAnnotation.model_json_schema(), ensure_ascii=False, indent=2
        )
        + "\n",
        "templates/annotation.json": example_annotation().model_dump_json(indent=2)
        + "\n",
        "gold-format.md": """<!-- Generated -->
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
""",
        "README.md": """<!-- Generated from hvs_catalog.evaluation.generate. -->
# Gold annotations

Use one [annotation](templates/annotation.json) and one adjacent `paper.pdf` per paper.
The example is fictional. See the [schema](schemas/annotation.json) and [format](gold-format.md).
These tools do not annotate papers, call models, migrate legacy data or commit Git.

```sh
hvs-catalog gold check --annotation /work/PAPER/annotation.json
hvs-catalog gold compare --before /work/old/annotation.json --after /work/new/annotation.json
hvs-catalog gold inspect-save --annotation /work/PAPER/annotation.json --target /private/data/PAPER
hvs-catalog gold save --annotation /work/PAPER/annotation.json --target /private/data/PAPER --expected-new SHA256
hvs-catalog gold read --target /private/data/PAPER
```

Create `data/` before publication. After reviewing the full annotation and PDF, obtain
human confirmation of the complete answer and save that exact content. For a revision,
pass `--expected-old SHA256` to both inspect-save and save. `--expected-new` is the exact
file-byte hash returned by inspect-save. These hashes reject changed content; they do
not record or prove human approval. Git submission remains a separate action.

Check verifies current science rules, PDF bytes and every evidence page, without text
extraction. Compare uses only unique exact shared identifiers to pair objects; ambiguous
or renamed rows remain unmatched. Conditions, evidence, notes and metadata remain in
the comparison. Neither command makes scientific judgements.

Publication locks the stable `data/` directory, stages files under `.work/gold-save/`,
and atomically publishes a complete new paper directory or replaces an existing
annotation. A revision cannot change the fixed PDF. Wrong paper IDs, symbolic links,
unknown layouts, stale hashes and material changes are rejected. An interrupted process
leaves either the old annotation or the complete new annotation; verify its hash before
retrying. Scratch directories can remain after a killed process and can be inspected
or removed after all writers stop. Do not replace the locked data directory concurrently.
No lock, backup, marker, metadata or approval file is added to a paper directory.

Scoring reads `annotation.json` directly; no export is needed. Its manifest fixes the
selected papers. Frozen scoring inputs retain the full annotation, exact source-file
hash and observed Git commit/status when available. A source file can be uncommitted;
its content hash remains authoritative. Historical reports retain their original inputs.
""",
    }


def write_gold_outputs(output):
    root = Path(output)
    # Only retire known generated contracts.
    for name in (
        "schemas/gold.answer.json",
        "schemas/gold.metadata.json",
        "schemas/gold.approval.json",
        "templates/gold-answer.json",
        "templates/gold-metadata.json",
        "templates/gold-approval.md",
    ):
        (root / name).unlink(missing_ok=True)
    outputs = build_gold_outputs()
    for name, content in outputs.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return outputs


def check_gold_outputs(output):
    root = Path(output)
    expected = build_gold_outputs()
    problems = [
        f"Outdated Gold contract: {name}"
        for name, content in expected.items()
        if not (root / name).is_file()
        or (root / name).read_text(encoding="utf-8") != content
    ]
    problems.extend(
        f"Unexpected Gold contract: {p.relative_to(root)}"
        for p in root.rglob("*")
        if p.is_file() and p.relative_to(root).as_posix() not in expected
    )
    return problems
