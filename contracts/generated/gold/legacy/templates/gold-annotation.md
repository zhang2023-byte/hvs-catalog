<!-- Generated -->
# Gold v1 migration working template

This is a complete synthetic JSON example of a v1 working draft, not an approved answer. For current formal Gold, use the [two-file format](../../gold-format.md) and its templates in ../../templates/. Replace the example science with the actual paper. Null metadata is intentionally unresolved and blocks formal saving; never fabricate identities, dates or hashes. The companion gold-annotation.json contains the same parseable document.

## Instructions

> Annotate one paper from its fixed PDF. Record the paper's own judgments and printed values verbatim: keep identifiers, numeric strings, units, conditions and error forms exactly as printed; never convert, complete, or average them. Cite where in the PDF you saw each statement using the registered PDF file, a free-text location description, an optional 1-based PDF page ordinal (the position in the file, not the printed page number), and an optional verbatim quote. Bind every piece of evidence to a registered file with its content hash. Mark unresolved science (author status, competing values) as pending with a reason instead of inventing an answer. Technical identifiers (contribution, conflict, exclusion, pending ids) are assigned by the tool; supply the scientific content and known attribution/material facts explicitly.

## Authorship and approval

provenance.prepared_by records this draft's human or AI maker, model for AI, timezone-aware prepared_at, action and description. It does not authorize saving. For a migrated annotation, preserve legacy.annotator and legacy.annotated_at verbatim; record a confirmed original human separately in original_expert with the original record as basis. Unknown historical identity/time must remain unknown with a reason. Do not classify an old annotator as human by guessing.

For a new annotation, original_expert stays null: the first human approval is the authority for original authorship. Saved answers carry original_approval_version=1 and provenance.json points to that version's approval.json. Later approvals never replace the original expert. Each revision records its own maker and human approval. The original human expert may be will, shunhong_deng, or another explicitly identified expert; these names are not defaults. This records declared identities, not authentication.

## Materials and technical fields

Set paper.version explicitly. material_correspondence records the checked PDF file IDs, fixed LaTeX file/archive name, hash and version, human checker, checked_at, basis and known differences. Until correspondence is checked the draft cannot be saved formally. Hashes establish file identity, not scientific equivalence. Differences require explicit acceptance and later evaluation-scope review. PDF and LaTeX files are read from material_root.

The JSON reader assigns omitted contribution_id, conflict_id, exclusion_id and item_id deterministically, preserving supplied IDs and rejecting duplicates. Supply document_id and registered file_id; record real file hashes. Gold revision numbers are assigned by the save tool and are independent of paper versions.

## Quantity codes

- `observed_phase_space.ra`
- `observed_phase_space.dec`
- `observed_phase_space.distance`
- `observed_phase_space.parallax`
- `observed_phase_space.proper_motion_ra`
- `observed_phase_space.proper_motion_dec`
- `observed_phase_space.radial_velocity`
- `derived_kinematics.galactocentric_x`
- `derived_kinematics.galactocentric_y`
- `derived_kinematics.galactocentric_z`
- `derived_kinematics.galactocentric_radius`
- `derived_kinematics.galactocentric_vx`
- `derived_kinematics.galactocentric_vy`
- `derived_kinematics.galactocentric_vz`
- `derived_kinematics.tangential_velocity`
- `derived_kinematics.galactic_rest_frame_velocity`
- `bound_assessment.bound_probability`
- `bound_assessment.unbound_probability`

## Synthetic draft

```json
{
  "schema": {
    "name": "evaluation.hvs_contribution_gold",
    "version": 1
  },
  "kind": "draft",
  "document_id": "gold-synthetic-example",
  "paper": {
    "paper_key": "synthetic-example",
    "arxiv_id": null,
    "version": null
  },
  "rules": {
    "version": "0.1.0",
    "sha256": "04faec79059d08f636aa91b4525a3cf10123d68673dec6b789d9b8c5c33377c4"
  },
  "files": [
    {
      "file_id": "pdf-1",
      "name": "paper.pdf",
      "sha256": null
    }
  ],
  "status": "contributions_found",
  "contributions": [
    {
      "contribution_id": "c1",
      "legacy_index": null,
      "identifiers": [
        {
          "value": "SYN-A",
          "evidence": [
            {
              "file_id": "pdf-1",
              "location": "Table 2, row SYN-A",
              "page": 3,
              "quote": ""
            }
          ]
        }
      ],
      "contribution_type": "candidates_found",
      "contribution_summary": "Synthetic candidate report.",
      "contribution_evidence": [
        {
          "file_id": "pdf-1",
          "location": "Table 2, row SYN-A",
          "page": 3,
          "quote": ""
        }
      ],
      "paper_boundness": {
        "status": "unbound",
        "legacy_status": null,
        "evidence": [
          {
            "file_id": "pdf-1",
            "location": "Table 2, row SYN-A",
            "page": 3,
            "quote": ""
          }
        ]
      },
      "quantities": [
        {
          "quantity": "observed_phase_space.radial_velocity",
          "values": [
            {
              "value": "612.0",
              "error": "8.0",
              "lower_error": null,
              "upper_error": null,
              "unit": "km/s",
              "limit_kind": "none",
              "range_lower": null,
              "range_upper": null,
              "coordinate_format": null,
              "condition": "Synthetic heliocentric measurement",
              "interval_kind": null,
              "coverage": null,
              "lower_quantile": null,
              "upper_quantile": null,
              "error_components": [],
              "representation": null,
              "source": "this_paper",
              "source_note": null,
              "evidence": [
                {
                  "file_id": "pdf-1",
                  "location": "Table 2, row SYN-A",
                  "page": 3,
                  "quote": ""
                }
              ],
              "context_evidence": []
            }
          ]
        }
      ],
      "quantity_conflicts": []
    }
  ],
  "reviewed_exclusions": [],
  "provenance": {
    "origin": "new_annotation",
    "legacy": null,
    "note": "",
    "original_expert": null,
    "original_approval_version": null,
    "prepared_by": null
  },
  "material_correspondence": {
    "status": "pending",
    "pdf_file_ids": [],
    "latex": null,
    "checked_by": null,
    "checked_at": null,
    "basis": "",
    "differences": []
  },
  "pending": []
}
```

Missing evidence, conditions or hashes must be filled from known materials, not guessed. Draft pending categories are missing_info, structural_gap and science_review. Structural and material gaps block saving. Explicitly accepted scientific pending items retain their complete descriptions in the formal provenance. Conflicts preserve competing values and evidence.
