<!-- Generated -->
# Human approval record

This template applies to immutable version saves. An authorized local correction
edits the existing answer and metadata in place and records the existing human decision;
it does not need a new preparation approval. See the [Gold maintenance design](../../../../docs/design/evaluation.md).

For the current two-file Gold format, review both answer.json and metadata.json, plus the check report.
Set scientific_review=true only after a human expert has reviewed the scientific
content of that exact pair. The approved manifest hashes both files, fixed materials,
rules and target. The approval is saved as a protected operation sidecar; the formal
version directory contains answer.json and metadata.json only.

For a historical v1 save, the prepared package included answer and provenance.
After reviewing the prepared answer, provenance and check report, a human expert supplies
an approval matching that preparation. This document is guidance, not an approval.
Use [the approval schema](../schemas/gold.approval.json) for the full typed contract.

| Field | Required content |
|---|---|
| approved_by | Explicit human expert identifier |
| approver_kind | human (AI approval is rejected) |
| approved_at | Actual approval time, ISO 8601 with timezone |
| document_id, target_path | Exact document and target from the manifest |
| manifest_sha256 | Hash of the exact reviewed preparation |
| expected_old | Exact predecessor reference, or null for first save |
| rules_version, rules_sha256 | Exact rules reference from the manifest |
| basis | Human review and approval rationale |
| scientific_review | true for a current two-file save after reviewing the exact answer and metadata; false otherwise |
| accepted_pending | Each explicitly accepted scientific pending ID |

Any change to the answer, maker, original attribution, materials or target requires a new
preparation and approval. Historical v1 keeps approval.json in its version directory;
v2 keeps the approval in operations. Never replace an old approval with a later
expert's name. No identity provider or digital signature is implied.
