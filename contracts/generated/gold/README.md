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
| Git commit | Authorized historical content; no new vN directories or version index |
| Old saved `vN/` directories | Historical layout, rejected by the current writer; no migration performed |
| Paper or source `version` | The version of the paper or source material |
| Rules `version` | The scientific rule set used for the annotation |

These numbers advance independently. Moving contract files does not change a
saved answer, its metadata, its hashes or its Gold revision.

## Current save and manual recovery

Use one directory per paper, e.g. private-repo/data/2501.00001, not the repository
root. It contains gold-target.json, .lock, answer.json, metadata.json and approval.json.
Only new/empty or exactly compatible targets are accepted. A selected parent alias
(such as macOS /var) resolves to the real root, which the approval binds. The target
itself (including dangling symlinks) and internal managed paths must not be symlinks.
A parent alias redirected to a different root cannot reuse approval. Old layouts are
not read or migrated. The tool does not migrate existing private-repository data or
update repository documentation.

Python: organize drafts with compact_gold_annotation / write_compact_gold_pair;
then check_save(pair_path=..., material_root=..., target=GoldTarget(...),
expected_old=None or the current fingerprint). Have a human supply SaveApproval
with the returned binding and binding.sha256. Call approved_save with the same
pair/material/target and approval. Revisions use a separate work copy, preserve
original attribution, and include metadata.revision. GoldTarget.read_current()
checks the current approval; fingerprint() returns the pair digest. Plain format
parsers read drafts and do not certify saved targets.

CLI: gold inspect-save --pair WORK/answer.json --material-root MATERIALS --target
TARGET [--expected-old DIGEST] prints binding and binding_sha256 without writing.
After human approval, gold save --pair WORK/answer.json --material-root MATERIALS
--target TARGET --approval APPROVAL.json saves. gold read --target TARGET checks
current content and prints its fingerprint. Old prepare/query/git-refresh commands
and operation-id/preparation arguments are removed. No tool runs Git commands.

Interrupted save leaves .save-backup and refuses formal reads/writes. Stop writers,
hold the target .lock (SpaceLock), and preserve the entire incident directory before
manual recovery. The backup contains complete old/ and new/ triplets. Restore all
three files from one set, never mix them. For an empty old/ set (first save), remove
the three current files to restore the initialized empty target. Check restored
answer/metadata with read_compact_gold_pair, and validate approval.binding against
the authorized binding: hash the exact restored answer/metadata bytes, compare them
to approval.binding.answer_sha256/metadata_sha256, check the absolute target path,
validate the human approval fields and binding_sha256 (SaveBinding.sha256), and
check materials/pending via check_compact_gold_pair. Do not call inspect-save against
the interrupted target: it intentionally refuses.

After restoring and verifying that complete set (or the empty rollback), inspect
also the target root for atomic-write temporary leftovers: a process can stop after
writing .metadata.json.tmp-* (or an answer/approval temporary file), before replacing
the current file. These leftovers still block formal reads/writes, even when the
restored triplet is valid. Manually confirm each leftover's exact path and contents
against the preserved incident and the selected save's old/new files. A filename
pattern alone is not proof of ownership or authorization to delete. Preserve and
move each confirmed leftover to an incident-storage location outside the target,
without overwriting existing files. Do not use wildcard deletion, automatically
adopt leftovers, or clear other unknown files; if ownership cannot be established,
retain the blocked target for further manual investigation. Keep the complete
incident copy and moved residue as recovery evidence.

Only after this verification and manual move-out may .save-backup be removed;
then gold read must succeed (or report no answer for an empty rollback).
If cleanup was interrupted and backup sets are
incomplete, verify the complete current triplet against its approval before removing
the remnant. Unknown temporary siblings from interruption before backup publication
are not adopted or automatically cleaned. Preserve them for operator inspection.
The tool does not replay recovery or repair arbitrary damage. The backup covers the
previous uncommitted pair too; successful saves remove only this invocation's backup.
