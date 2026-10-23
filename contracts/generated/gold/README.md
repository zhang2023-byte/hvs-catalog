<!-- Generated from hvs_catalog.evaluation.generate. -->
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
