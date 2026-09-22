<!-- Generated -->
# Human approval record

After reviewing the prepared answer, provenance and check report, a human expert supplies
an approval matching that preparation. This document is guidance, not an approval.
Use schemas/gold.approval.json for the full typed contract.

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
| accepted_pending | Each explicitly accepted scientific pending ID |

Any change to the answer, maker, original attribution, materials or target requires a new
preparation and approval. Each version keeps its own approval.json; never replace an old
approval with a later expert's name. No identity provider or digital signature is implied.
