<!-- Generated -->
# Gold v1 migration working template

This is a complete synthetic JSON example of a v1 working draft, not an approved answer. For current formal Gold, use the [two-file format](../../gold-format.md) and its templates in ../../templates/. Replace the example science with the actual paper. Null metadata is intentionally unresolved and blocks formal saving; never fabricate identities, dates or hashes. The companion gold-annotation.json contains the same parseable document.

## Instructions

> Annotate one paper from its fixed PDF. Record the paper's own judgments and printed values verbatim: keep identifiers, numeric strings, units, conditions and error forms exactly as printed; never convert, complete, or average them. Cite where in the PDF you saw each statement using the registered PDF file, a free-text location description, an optional 1-based PDF page ordinal (the position in the file, not the printed page number), and an optional verbatim quote. Bind every piece of evidence to a registered file with its content hash. Mark unresolved science (author status, competing values) as pending with a reason instead of inventing an answer. Technical identifiers (contribution, conflict, exclusion, pending ids) are assigned by the tool; supply the scientific content and known attribution/material facts explicitly.

## Authorship and approval

provenance.prepared_by records this draft's human or AI maker, model for AI, timezone-aware prepared_at, action and description. It does not authorize saving. For a migrated annotation, preserve legacy.annotator and legacy.annotated_at verbatim; record a confirmed original human separately in original_expert with the original record as basis. Unknown historical identity/time must remain unknown with a reason. Do not classify an old annotator as human by guessing.

For a new working draft, original_expert may remain null. Before current-format conversion, explicitly identify its actual original annotator and reviewer; final approval is a separate human declaration for the concrete pair. No v1 formal save or provenance writer is provided. Revisions preserve original annotation identity. The original human expert may be will, shunhong_deng, or another explicitly identified expert; these names are not defaults. This records declared identities, not authentication.

## Materials and technical fields

Set paper.version explicitly. material_correspondence records the checked PDF file IDs, human checker, checked_at, basis and known differences. Until correspondence is checked the draft cannot be saved formally. Hashes establish file identity, not scientific equivalence. Differences require explicit acceptance and later evaluation-scope review. Fixed PDFs are read from material_root.

The JSON reader assigns omitted contribution_id, conflict_id, exclusion_id and item_id deterministically, preserving supplied IDs and rejecting duplicates. Supply document_id and registered file_id; record real file hashes. This draft is read-only to the formal writer; explicitly convert to the current pair before review/save. Git history, format versions, paper versions and rules versions are distinct.

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
    "version": "0.3.0",
    "sha256": "1fb10e90d3659e646a1bf75c2a7e24c0fbae73988f607e4714159f6e53bbfa68"
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
    "checked_by": null,
    "checked_at": null,
    "basis": "",
    "differences": []
  },
  "pending": []
}
```

Missing evidence, conditions or hashes must be filled from known materials, not guessed. Draft pending categories are missing_info, structural_gap and science_review. Structural and material gaps block readiness. Conversion requires explicit pending dispositions; retained scientific uncertainty belongs in current answer.unresolved, not a separate formal provenance file. Conflicts preserve competing values and evidence.
