<!-- Generated -->
# Human approval record

New answers and revisions use the same current-pair save. Organize the compact pair
before review; use check_save / gold inspect-save for a read-only binding summary.
This document is guidance, not approval. See the [Gold maintenance design](../../../../docs/design/evaluation.md).

Review answer.json, metadata.json and all reported checks. Set scientific_review=true
only after a human expert has reviewed this exact content. The binding hashes both
files (therefore their rules, material fingerprints and attribution), the absolute
target and expected old pair fingerprint. Save approval.json alongside the current
pair. Metadata review alone is not save authorization.
Use [the approval schema](../schemas/gold.approval.json) for the typed contract.
Old v4 inputs and v1 drafts remain read-only; explicitly convert them to a compact
pair before review. Old versioned targets and approvals are not adopted.

| Field | Required content |
|---|---|
| approved_by | Explicit human expert identifier |
| approver_kind | human (AI approval is rejected) |
| approved_at | Actual approval time, ISO 8601 with timezone |
| binding | Entire check_save binding, including target, files, rules, science_pending and expected_old |
| binding_sha256 | check_save binding.sha256; CLI inspect-save prints it |
| binding.expected_old | Current pair fingerprint from GoldTarget.fingerprint / gold read, or null for new Gold |
| basis | Human review and approval rationale |
| scientific_review | true only after human review of the exact pair |
| accepted_pending | Each explicitly accepted scientific pending ID |

Changed content, target or predecessor requires new approval. Revisions preserve
original annotation identity and record revision actors/date/basis and the previous
answer hash in metadata. Earlier approval is not refreshed to cover new content.
No identity provider or digital signature is implied. Tools never execute Git.
