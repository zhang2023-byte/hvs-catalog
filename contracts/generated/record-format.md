<!-- Generated -->
# Data record format

This document is generated from the shared Pydantic definitions in `hvs_catalog.records`. Do not edit it by hand; regenerate with `uv run python tools/render_record_contracts.py`. JSON Schemas for every record type and step submission live under `schemas/`; step templates for extractors live under `templates/`.

### Record envelope

Every record is a JSON object with the following outer fields; the payload lives under `data`. Field names, status codes and this documentation are English. New summaries, questions and precedent notes are English; printed identifiers, values and evidence excerpts retain their original form.

| Field | Type | Required | Notes |
|---|---|---|---|
| `schema` | `{name, version}` | yes | `name` is one of the record types below; `version` is `1`. Unknown names or versions are rejected. |
| `record_id` | string | yes | Stable technical identifier; revisions of one record share it. |
| `revision` | integer >= 1 | yes | Strictly increasing per record_id; revisions are never overwritten in place. |
| `created_at` | timestamp | yes | Must carry a timezone. |
| `execution_id` | string, nullable | yes | Links the creating execution; null only for manual imports. |
| `data` | record payload | yes | Typed by `schema.name`; undeclared fields are rejected. |

| Record type (`schema.name`) | Envelope model |
|---|---|
| `decision` | `DecisionRecord` |
| `evidence` | `EvidenceRecord` |
| `external_record` | `ExternalRecord` |
| `object` | `ObjectRecord` |
| `paper` | `PaperRecord` |
| `question` | `QuestionRecord` |
| `release` | `ReleaseRecord` |
| `roster` | `RosterRecord` |
| `run` | `RunRecord` |
| `source_package` | `SourcePackageRecord` |

### Quantity scope (18 quantities)

The quantity code is the legacy path of the approved science rules; the definitions below are generated from the same source.

| Code | Name | Definition |
|---|---|---|
| `observed_phase_space.ra` | Right ascension | Right ascension reported or adopted by the authors. Preserve the original angular or hour-angle representation and its epoch. |
| `observed_phase_space.dec` | Declination | Declination reported or adopted by the authors. Preserve its sign, original representation, and epoch. |
| `observed_phase_space.distance` | Heliocentric distance | Heliocentric distance given by the authors. If expressed as a logarithm of distance or a distance modulus, preserve the original value and identify the representation without converting it. |
| `observed_phase_space.parallax` | Parallax | An object's measured, inferred, or predicted parallax, or a parallax value actually adopted as an assumption or prior in its analysis. Preserve zero and negative values and record the method and purpose. Exclude calibration offsets and general selection thresholds from this field. |
| `observed_phase_space.proper_motion_ra` | Proper motion in right ascension | The right-ascension component of proper motion in equatorial coordinates. Preserve the authors' μ_α or μ_α* (including cos δ) convention without conversion. |
| `observed_phase_space.proper_motion_dec` | Proper motion in declination | The declination component of proper motion in equatorial coordinates. Preserve the epoch and the authors' convention. |
| `observed_phase_space.radial_velocity` | Heliocentric or barycentric radial velocity | Line-of-sight velocity reported in the heliocentric or barycentric frame. Do not enter local-standard-of-rest (LSR) or Galactic-standard-of-rest (GSR) line-of-sight velocities here. |
| `derived_kinematics.galactocentric_x` | Galactocentric x | Galactocentric Cartesian position x at the reference state. Preserve the authors' axis directions and frame description. |
| `derived_kinematics.galactocentric_y` | Galactocentric y | Galactocentric Cartesian position y at the reference state. Preserve the authors' axis directions and frame description. |
| `derived_kinematics.galactocentric_z` | Galactocentric z | Galactocentric Cartesian position z at the reference state. Preserve the authors' axis directions and frame description. |
| `derived_kinematics.galactocentric_radius` | Three-dimensional Galactocentric distance | The three-dimensional distance r from the object to the Galactic center at the reference state. Do not enter cylindrical radius R here. If the type of distance is unclear, refer it to an expert. |
| `derived_kinematics.galactocentric_vx` | Galactocentric vx | Galactocentric Cartesian velocity component vx at the reference state. Preserve the frame convention. |
| `derived_kinematics.galactocentric_vy` | Galactocentric vy | Galactocentric Cartesian velocity component vy at the reference state. Preserve the frame convention. |
| `derived_kinematics.galactocentric_vz` | Galactocentric vz | Galactocentric Cartesian velocity component vz at the reference state. Preserve the frame convention. |
| `derived_kinematics.tangential_velocity` | Heliocentric tangential speed | The magnitude of the velocity in the plane of the sky relative to the Sun, at the present or reference state. Do not enter Galactocentric tangential or cylindrical velocities here. |
| `derived_kinematics.galactic_rest_frame_velocity` | Three-dimensional Galactic rest-frame speed | The total three-dimensional speed in a Galactic or Galactocentric rest frame at the present or reference state. Exclude velocity components, LSR/GSR line-of-sight velocities, escape speeds, and speeds at other orbital times. |
| `bound_assessment.bound_probability` | Galactic bound probability | A numerical Galactic bound probability reported by the authors. Preserve its fraction or percentage representation. Do not calculate it as one minus the unbound probability. |
| `bound_assessment.unbound_probability` | Galactic unbound probability | A numerical Galactic unbound or escape probability reported by the authors. Do not substitute an origin probability, classification confidence, or simulation count. |

### Authors' overall assessment states (S04)

| State | Definition |
|---|---|
| `no_overall_conclusion` | The paper gives no overall assessment of the object: either boundness is not discussed, or it is discussed without an overall conclusion or preference. |
| `unbound` | The authors explicitly give an overall conclusion that the object is unbound from or escaping the Galaxy. |
| `possibly_unbound` | The authors describe the object overall as possibly or likely unbound, or explicitly favor an unbound interpretation after discussing different cases. |
| `possibly_bound` | The authors overall favor a Galactic bound interpretation while still considering an unbound interpretation possible. |
| `bound` | The authors explicitly give an overall bound conclusion. Such an object is not included as candidates_found; an object eligible as follow_up of a historical candidate may have this state. |

Rules version: `0.1.0` (sha256 `04faec79059d08f636aa91b4525a3cf10123d68673dec6b789d9b8c5c33377c4`).

## Field definitions

Required nullable fields must be present with `null`; only fields marked optional may be omitted in step submissions. List fields state their minimum item count; a required list must be provided explicitly even when empty.

### Common reference types

#### SchemaTag

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `name` | `string` | yes | minLength 1 |  |
| `version` | `integer` | yes | minimum 1 |  |

#### RecordRef

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `record_id` | `string` | yes | minLength 1 |  |
| `revision` | `integer` | yes | minimum 1 |  |

#### ItemRef

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `record_id` | `string` | yes | minLength 1 |  |
| `revision` | `integer` | yes | minimum 1 |  |
| `item_id` | `string` | yes | minLength 1 |  |

#### ArtifactRef

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `path` | `string` | yes | minLength 1 |  |
| `sha256` | `string` | yes | pattern `^[0-9a-f]{64}$` |  |

#### GapRef

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `source_ref` | `RecordRef` | yes |  |  |
| `gap_id` | `string` | yes | minLength 1 |  |

### Source package and evidence

#### SyntheticOrigin

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `kind` | `synthetic` | yes |  |  |

#### ArxivOrigin

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `kind` | `arxiv` | yes |  |  |
| `arxiv_id` | `string` | yes | minLength 1 |  |
| `retrieval` | `string` | yes | minLength 1 | How and when the source package was retrieved, e.g. the download basis. |

#### SourceFile

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `file_id` | `string` | yes | minLength 1 |  |
| `path` | `string` | yes | minLength 1 |  |
| `sha256` | `string` | yes | pattern `^[0-9a-f]{64}$` |  |
| `media_type` | `string` | yes | minLength 1 |  |
| `role` | `string` | yes | minLength 1 |  |
| `read_state` | `ready` / `unparsed` / `unreadable` | yes |  |  |
| `derived_from` | list of `string` | yes |  |  |

#### AffectedItem

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `item_id` | `string` \| `null` | yes |  | Contribution item affected by the gap, when identifiable. Nullable: absent content is `null`, never a default value. |
| `quantity` | `string` \| `null` | yes |  | Quantity path affected by the gap, when the scope names one. Nullable: absent content is `null`, never a default value. |
| `judgment` | `inclusion` / `identity` \| `null` | yes |  | Roster-level judgment the gap affects: inclusion (whether the object belongs to the roster) or identity (whether printed names denote one object). Mutually exclusive with quantity in one item; null when the item names quantities only. Nullable: absent content is `null`, never a default value. |

#### AffectedTargets

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `scope` | `known` / `unknown` | yes |  |  |
| `items` | list of `AffectedItem` | yes | minItems 0 |  |

#### Gap

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `gap_id` | `string` | yes | minLength 1 |  |
| `reason` | `missing` / `unreadable` / `unparsed` / `external_not_included` | yes |  |  |
| `description` | `string` | yes | minLength 1 |  |
| `file_id` | `string` \| `null` | yes |  | Omitted for gaps without a specific package file. Nullable: absent content is `null`, never a default value. |
| `mention_evidence_refs` | list of `RecordRef` | yes | minItems 0 |  |
| `affected_targets` | `AffectedTargets` | yes |  |  |

#### SourcePackageData

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `paper_key` | `string` | yes | minLength 1 |  |
| `origin` | `SyntheticOrigin` \| `ArxivOrigin` | yes |  |  |
| `source_version` | `string` | yes | minLength 1 |  |
| `files` | list of `SourceFile` | yes | minItems 0 |  |
| `gaps` | list of `Gap` | yes | minItems 0 |  |

#### TextLocator

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `kind` | `text` | yes |  |  |
| `start_line` | `integer` | yes | minimum 1 |  |
| `end_line` | `integer` | yes | minimum 1 |  |

#### TableCellLocator

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `kind` | `table_cell` | yes |  |  |
| `row_key` | `object` | yes |  |  |
| `column` | `string` | yes | minLength 1 |  |
| `row_number` | `integer` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |

#### DataElementSelector

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `kind` | `string` | yes | minLength 1 |  |
| `value` | `string` | yes | minLength 1 |  |

#### DataElementDecoder

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `name` | `string` | yes | minLength 1 |  |
| `encoding` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |

#### DataElementLocator

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `kind` | `data_element` | yes |  |  |
| `selector` | `DataElementSelector` | yes |  |  |
| `decoder` | `DataElementDecoder` | yes |  |  |

#### EvidenceData

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `paper_key` | `string` | yes | minLength 1 |  |
| `source_ref` | `RecordRef` | yes |  |  |
| `file_id` | `string` | yes | minLength 1 |  |
| `locator` | `TextLocator` \| `TableCellLocator` \| `DataElementLocator` | yes |  |  |
| `excerpt` | `string` | yes | minLength 1 |  |

### Paper and object contributions

#### PaperMetadata

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `arxiv_id` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `version` | `string` | yes | minLength 1 |  |
| `title` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `authors` | list of `string` | yes | minItems 0 |  |

#### DirectEvidencePart

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `part` | `string` | yes | minLength 1 |  |
| `evidence_ref` | `RecordRef` | yes |  |  |
| `raw_fragment` | `string` | yes | minLength 1 | The printed fragment of this component; must equal the saved numeric string of that component. |

#### CoverageReport

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `value` | `string` | yes | minLength 1 |  |
| `unit` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `direct_evidence` | list of `DirectEvidencePart` | yes | minItems 1 |  |

#### QuantileReport

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `value` | `string` | yes | minLength 1 |  |
| `unit` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `direct_evidence` | list of `DirectEvidencePart` | yes | minItems 1 |  |

#### ErrorComponent

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `name` | `string` | yes | minLength 1 |  |
| `error` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `lower_error` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `upper_error` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `direct_evidence` | list of `DirectEvidencePart` | yes | minItems 1 |  |

#### MeasuredValue

A saved numeric expression; required and optional fields follow this model.

Unreported nullable components remain null; reported components retain their
printed form and direct evidence. Step filling uses ValueSubmission instead.

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `value_id` | `string` | yes | minLength 1 |  |
| `value` | `string` \| `null` | yes |  | Printed value string; null only for pure intervals. Nullable: absent content is `null`, never a default value. |
| `error` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `lower_error` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `upper_error` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `unit` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `limit_kind` | `none` / `lower_limit` / `upper_limit` / `range` | yes |  |  |
| `range_lower` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `range_upper` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `lower_inclusive` | `boolean` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `upper_inclusive` | `boolean` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `interval_kind` | `ordinary_range` / `prior_support` / `confidence` / `credible` / `statistical_interval` / `unspecified` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `coverage` | `CoverageReport` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `lower_quantile` | `QuantileReport` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `upper_quantile` | `QuantileReport` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `error_components` | list of `ErrorComponent` | yes | minItems 0 |  |
| `condition` | `string` | yes | minLength 1 |  |
| `frame` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `epoch` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `coordinate_format` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `representation` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `purpose` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `source` | `this_paper` / `prior_work` / `unclear` | yes |  |  |
| `source_note` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `author_usage` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `direct_evidence` | list of `DirectEvidencePart` | yes | minItems 1 |  |
| `context_evidence_refs` | list of `RecordRef` | yes | minItems 0 |  |

#### IdentifierEntry

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `value` | `string` | yes | minLength 1 |  |
| `evidence_refs` | list of `RecordRef` | yes | minItems 1 |  |

#### ContributionSummary

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `text` | `string` | yes | minLength 1 |  |
| `evidence_refs` | list of `RecordRef` | yes | minItems 1 |  |

#### ContributionTypeEntry

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `value` | `candidates_found` / `follow_up` | yes |  |  |
| `evidence_refs` | list of `RecordRef` | yes | minItems 1 |  |

#### PaperBoundness

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `status` | `no_overall_conclusion` / `unbound` / `possibly_unbound` / `possibly_bound` / `bound` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `evidence_refs` | list of `RecordRef` | yes | minItems 0 |  |
| `issue_refs` | list of `RecordRef` | yes | minItems 0 |  |
| `gap_refs` | list of `GapRef` | yes | minItems 0 |  |

#### ConditionalAssessment

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `condition` | `string` | yes | minLength 1 |  |
| `statement` | `string` | yes | minLength 1 |  |
| `evidence_refs` | list of `RecordRef` | yes | minItems 1 |  |

#### QuantityGroup

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `quantity` | `string` | yes | minLength 1 |  |
| `values` | list of `MeasuredValue` | yes | minItems 1 |  |

#### QuantityConflict

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `conflict_id` | `string` | yes | minLength 1 |  |
| `quantity` | `string` | yes | minLength 1 |  |
| `reason` | `string` | yes | minLength 1 |  |
| `alternatives` | list of `MeasuredValue` | yes | minItems 2 |  |
| `issue_ref` | `RecordRef` | yes |  |  |

#### QuantityCoverageEntry

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `quantity` | `string` | yes | minLength 1 |  |
| `state` | `not_checked` / `partial` / `complete` | yes |  |  |
| `checked_file_ids` | list of `string` | yes | minItems 0 |  |
| `gap_refs` | list of `GapRef` | yes | minItems 0 |  |
| `issue_refs` | list of `RecordRef` | yes | minItems 0 |  |

#### PrecedentApproval

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `approved_by` | `string` | yes | minLength 1 |  |
| `approved_at` | `string` | yes |  |  |

#### PrecedentSnapshotRef

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `precedent_id` | `string` | yes | minLength 1 |  |
| `version` | `string` | yes | minLength 1 |  |
| `text` | `ArtifactRef` | yes |  |  |
| `approval` | `PrecedentApproval` | yes |  |  |

#### PrecedentUse

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `precedent_ref` | `PrecedentSnapshotRef` | yes |  |  |
| `applies_to` | list of `ItemRef` | yes | minItems 1 |  |
| `explanation` | `string` | yes | minLength 1 |  |

#### ObjectContribution

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `item_id` | `string` | yes | minLength 1 |  |
| `roster_item_id` | `string` | yes | minLength 1 |  |
| `identifiers` | list of `IdentifierEntry` | yes | minItems 1 |  |
| `contribution_summary` | `ContributionSummary` | yes |  |  |
| `contribution_type` | `ContributionTypeEntry` | yes |  |  |
| `paper_boundness` | `PaperBoundness` | yes |  |  |
| `conditional_assessments` | list of `ConditionalAssessment` | yes | minItems 0 |  |
| `quantities` | list of `QuantityGroup` | yes | minItems 0 |  |
| `quantity_conflicts` | list of `QuantityConflict` | yes | minItems 0 |  |
| `quantity_coverage` | list of `QuantityCoverageEntry` | yes |  |  |
| `precedent_uses` | list of `PrecedentUse` | yes | minItems 0 |  |

#### PaperData

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `paper_key` | `string` | yes | minLength 1 |  |
| `metadata` | `PaperMetadata` | yes |  |  |
| `source_ref` | `RecordRef` | yes |  |  |
| `roster_ref` | `RecordRef` | yes |  |  |
| `object_contributions` | list of `ObjectContribution` | yes | minItems 0 |  |

### Roster

#### RosterCoverage

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `state` | `not_checked` / `partial` / `complete` | yes |  |  |
| `checked_file_ids` | list of `string` | yes | minItems 0 |  |
| `gap_refs` | list of `GapRef` | yes | minItems 0 |  |

#### RosterIdentifier

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `value` | `string` | yes | minLength 1 |  |
| `evidence_refs` | list of `RecordRef` | yes | minItems 1 |  |

#### RosterEntry

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `item_id` | `string` | yes | minLength 1 |  |
| `identifiers` | list of `RosterIdentifier` | yes | minItems 1 |  |
| `disposition` | `included` / `excluded` / `pending` | yes |  |  |
| `evidence_refs` | list of `RecordRef` | yes | minItems 0 |  |
| `issue_refs` | list of `RecordRef` | yes | minItems 0 |  |
| `contribution_id` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |

#### UnresolvedGroup

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `group_id` | `string` | yes | minLength 1 |  |
| `original_range` | `string` | yes | minLength 1 | The printed range expression, preserved as is. |
| `evidence_refs` | list of `RecordRef` | yes | minItems 1 |  |
| `known_member_item_ids` | list of `string` | yes | minItems 0 |  |
| `reason` | `string` | yes | minLength 1 |  |

#### RosterData

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `paper_key` | `string` | yes | minLength 1 |  |
| `source_ref` | `RecordRef` | yes |  |  |
| `method` | `direct` / `candidate_review` | yes |  |  |
| `coverage` | `RosterCoverage` | yes |  |  |
| `entries` | list of `RosterEntry` | yes | minItems 0 |  |
| `unresolved_groups` | list of `UnresolvedGroup` | yes | minItems 0 |  |
| `issue_refs` | list of `RecordRef` | yes | minItems 0 |  |

### Objects and external records

#### ContributionLink

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `target_ref` | `RecordRef` | yes |  |  |
| `item_id` | `string` | yes | minLength 1 |  |
| `state` | `confirmed` / `pending` / `rejected` | yes |  |  |
| `basis` | `string` | yes | minLength 1 |  |
| `evidence_refs` | list of `RecordRef` | yes | minItems 0 |  |
| `decision_refs` | list of `RecordRef` | yes | minItems 0 |  |

#### ExternalLink

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `target_ref` | `RecordRef` | yes |  |  |
| `entry_id` | `string` | yes | minLength 1 |  |
| `state` | `confirmed` / `pending` / `rejected` | yes |  |  |
| `basis` | `string` | yes | minLength 1 |  |
| `evidence_refs` | list of `RecordRef` | yes | minItems 0 |  |
| `decision_refs` | list of `RecordRef` | yes | minItems 0 |  |

#### ObjectChange

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `change_id` | `string` | yes | minLength 1 |  |
| `kind` | `merge` / `split` / `revision` | yes |  |  |
| `before_refs` | list of `RecordRef` | yes | minItems 0 |  |
| `after_refs` | list of `RecordRef` | yes | minItems 0 |  |
| `reason` | `string` | yes | minLength 1 |  |
| `basis_refs` | list of `RecordRef` | yes | minItems 1 |  |

#### ObjectData

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `object_id` | `string` | yes | minLength 1 |  |
| `display_name` | `string` | yes | minLength 1 |  |
| `aliases` | list of `string` | yes | minItems 0 |  |
| `contribution_links` | list of `ContributionLink` | yes | minItems 0 |  |
| `external_links` | list of `ExternalLink` | yes | minItems 0 |  |
| `changes` | list of `ObjectChange` | yes | minItems 0 |  |

#### ExternalField

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `name` | `string` | yes | minLength 1 |  |
| `raw_value` | `string` | yes | minLength 1 |  |
| `unit` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `response_selector` | `string` | yes | minLength 1 |  |

#### ExternalEntry

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `entry_id` | `string` | yes | minLength 1 |  |
| `fields` | list of `ExternalField` | yes | minItems 1 |  |

#### ExternalRecordData

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `service` | `string` | yes | minLength 1 |  |
| `catalog_version` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `queried_at` | `string` | yes |  |  |
| `query` | `object` | yes |  |  |
| `raw_response` | `ArtifactRef` | yes |  |  |
| `entries` | list of `ExternalEntry` | yes | minItems 0 |  |

### Questions and decisions

#### RulesRef

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `version` | `string` | yes | minLength 1 |  |
| `sha256` | `string` | yes | pattern `^[0-9a-f]{64}$` |  |

#### QuestionData

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `category` | `inclusion` / `identity` / `author_assessment` / `numeric_conflict` / `quantity_attribution` / `interpretation` | yes |  |  |
| `target_refs` | list of `ItemRef` | yes | minItems 1 |  |
| `source_refs` | list of `RecordRef` | yes | minItems 1 |  |
| `rules_ref` | `RulesRef` | yes |  |  |
| `prompt` | `string` | yes | minLength 1 |  |
| `evidence_refs` | list of `RecordRef` | yes | minItems 0 |  |
| `gap_refs` | list of `GapRef` | yes | minItems 0 |  |
| `context_refs` | list of `RecordRef` | yes | minItems 0 |  |
| `fingerprint` | `string` | yes | pattern `^[0-9a-f]{64}$` |  |
| `status` | `open` / `answered` / `superseded` | yes |  |  |
| `decision_refs` | list of `RecordRef` | yes | minItems 0 |  |

#### Actor

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `kind` | `human` / `model` | yes |  |  |
| `id` | `string` | yes | minLength 1 |  |

#### PatchChange

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `field` | `string` | yes | minLength 1 |  |
| `value` | `value` | yes |  |  |

#### DecisionPatch

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `target` | `ItemRef` | yes |  |  |
| `changes` | list of `PatchChange` | yes | minItems 1 |  |

#### DecisionData

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `question_ref` | `RecordRef` | yes |  |  |
| `question_fingerprint` | `string` | yes | pattern `^[0-9a-f]{64}$` |  |
| `actor` | `Actor` | yes |  |  |
| `decided_at` | `string` | yes |  |  |
| `outcome` | `resolved` / `unresolved` | yes |  |  |
| `answer_original` | `string` | yes | minLength 1 |  |
| `answer_en` | `string` | yes | minLength 1 |  |
| `reason` | `string` | yes | minLength 1 |  |
| `evidence_refs` | list of `RecordRef` | yes | minItems 0 |  |
| `result_patch` | `DecisionPatch` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |

### Run and release manifests

#### Usage

Usage of a call or a whole run; unknown token counts stay null.

``cost``/``currency`` are omittable: runs no longer compute, fill or
compare monetary costs; unfilled fields are not written to new record
JSON. They remain readable for historical data with values — see the
cost compatibility boundary in the architecture document.

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `tokens` | `integer` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `cost` | `number` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `currency` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |

#### ModelSpec

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `step_id` | `string` \| `null` | yes |  | Null for the whole-pipeline default model. Nullable: absent content is `null`, never a default value. |
| `provider` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `model` | `string` | yes | minLength 1 |  |
| `parameters` | `object` | yes |  |  |

#### InputSnapshot

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `source_refs` | list of `RecordRef` | yes | minItems 0 |  |
| `rules` | `RulesRef` | yes |  |  |
| `method` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `models` | list of `ModelSpec` | yes | minItems 0 |  |
| `code_version` | `string` | yes | minLength 1 |  |
| `dependency_versions` | `object` | yes |  |  |
| `expert_adjudication` | `boolean` | yes |  |  |
| `prompt_refs` | list of `ArtifactRef` | yes | minItems 0 |  |
| `example_refs` | list of `ArtifactRef` | yes | minItems 0 |  |
| `precedent_refs` | list of `PrecedentSnapshotRef` | yes | minItems 0 |  |
| `existing_decision_refs` | list of `RecordRef` | yes | minItems 0 |  |
| `options` | `object` | no |  |  |

#### Attempt

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `request` | `ArtifactRef` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `response` | `ArtifactRef` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `state` | `pending` / `succeeded` / `failed` | yes |  |  |
| `started_at` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `finished_at` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `usage` | `Usage` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `cost` | `number` \| `null` | no |  | Legacy monetary cost; new runs leave it unfilled and the serialized output omits it. Nullable: absent content is `null`, never a default value. |
| `currency` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `service_request_id` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |

#### StepRecord

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `step_id` | `string` | yes | minLength 1 |  |
| `state` | `created` / `pending` / `running` / `waiting_review` / `finished` / `failed` / `cancelled` | yes |  |  |
| `input_refs` | list of `RecordRef` | yes | minItems 0 |  |
| `attempts` | list of `Attempt` | yes | minItems 0 |  |
| `result_refs` | list of `RecordRef` | yes | minItems 0 |  |
| `error_refs` | list of `string` | yes | minItems 0 |  |

#### RunError

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `error_id` | `string` | yes | minLength 1 |  |
| `kind` | `string` | yes | minLength 1 |  |
| `message` | `string` | yes | minLength 1 |  |

#### ModelJudgment

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `step_id` | `string` | yes | minLength 1 |  |
| `judgment` | `string` | yes | minLength 1 |  |
| `created_at` | `string` | yes |  |  |

#### RunData

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `execution_id` | `string` | yes | minLength 1 |  |
| `mode` | `daily` / `evaluation` / `synthetic` | yes |  |  |
| `input_snapshot` | `InputSnapshot` | yes |  |  |
| `state` | `created` / `running` / `waiting_review` / `finished` / `failed` / `cancelled` | yes |  |  |
| `steps` | list of `StepRecord` | yes | minItems 0 |  |
| `result_refs` | list of `RecordRef` | yes | minItems 0 |  |
| `model_judgments` | list of `ModelJudgment` | yes | minItems 0 |  |
| `usage` | `Usage` | yes |  |  |
| `errors` | list of `RunError` | yes | minItems 0 |  |

#### Approval

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `approver` | `string` | yes | minLength 1 |  |
| `approved_at` | `string` | yes |  |  |
| `manifest_sha256` | `string` | yes | pattern `^[0-9a-f]{64}$` |  |
| `target` | `string` | yes | minLength 1 |  |

#### Channel

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `target` | `string` | yes | minLength 1 |  |
| `state` | `not_started` / `succeeded` / `failed` | yes |  |  |
| `receipt_ref` | `ArtifactRef` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `error` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |

#### ReleaseData

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `data_version` | `string` | yes | minLength 1 |  |
| `content_refs` | list of `RecordRef` | yes | minItems 0 |  |
| `artifacts` | list of `ArtifactRef` | yes | minItems 0 |  |
| `manifest_sha256` | `string` | yes | pattern `^[0-9a-f]{64}$` |  |
| `source_run_refs` | list of `RecordRef` | yes | minItems 1 |  |
| `code_version` | `string` | yes | minLength 1 |  |
| `change_summary` | `string` | yes | minLength 1 |  |
| `approval` | `Approval` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `channels` | list of `Channel` | yes | minItems 0 |  |

## Cross-record rules

The checks below cannot be expressed as field constraints alone; they are implemented in `hvs_catalog.records.validate` and run over whole record collections.

- **Reference resolution**: Every RecordRef, ItemRef and GapRef must resolve to a record that is present in the collection at exactly the referenced revision; a missing or stale revision is rejected instead of silently reading the latest one.

- **Typed references**: source_ref fields must point to source_package records; roster_ref to roster; evidence_refs to evidence; question_ref and issue_refs to question; decision_refs to decision; release source_run_refs to run; object change before/after refs to object records. Every field that carries scientific evidence is checked the same way, including identifier, summary, type, assessment, numeric, roster, question, decision, gap-mention and object link evidence. Object contribution links must point to a paper record that contains the referenced item; external links must point to an entry that exists in the referenced external record.

- **Paper consistency**: A paper record, its source package, its roster and every piece of evidence it cites — identifiers, summaries, types, assessments, numeric components and context evidence, on the exact referenced revision — must share the same paper_key. Evidence files must belong to the referenced source package. Question and decision evidence must belong to the paper of the question's source material.

- **Roster binding**: Included roster entries and paper object_contributions must correspond one to one through roster_item_id and contribution_id; excluded or pending entries carry no contribution.

- **Quantity coverage**: Each contribution carries exactly one quantity_coverage entry for every quantity in the approved 18-quantity scope. complete entries cannot reference material gaps and contradict any gap whose declared impact covers the entry's object and quantity (an empty item_id or quantity in a gap target means all objects or the whole object; an unknown scope covers everything). partial entries must reference every gap affecting the entry. checked files must exist in the paper's source package. Gap targets may also name a roster-level judgment — inclusion or identity — for known objects; such gaps keep the roster coverage unfinished (a complete roster claim is rejected, a partial one must reference the gap) while quantity-only known gaps leave the roster unaffected. not_checked honestly lists no checked files.

- **Numeric fragments**: Every raw_fragment of a numeric component must equal the saved numeric string of that component (value, errors, endpoints, coverage, quantiles, named error components) and occur inside the excerpt of the exact evidence revision it cites. Recorded numbers are the printed fragments; the validator never re-computes or re-formats them.

- **Conflict exclusivity**: A measurement pending in a quantity_conflict cannot also be saved as a confirmed value of the same quantity, not even under a new value_id or with the order of any evidence collection permuted — including the evidence inside named error components and interval reports: identity is established by the full expression with nested evidence sets normalized. Independent measurements — different conditions, sources or evidence — remain unaffected, and no unit conversion or scientific equivalence is inferred.

- **Decision binding**: The question's stored fingerprint is recomputed from its actual fixed content (category, targets, evidence, sources, rules); a question whose fingerprint does not match its content is rejected. A decision must carry the recomputed fingerprint of the exact question revision it answers, so a decision made for an older version of the question cannot be applied.

- **Patch validation**: A decision's result_patch must target one of the exact question targets (record, revision, item) that exists in the target record. Fields are resolved against the actual target item's model (a paper contribution or a roster entry): record-level fields cannot be reached through an item target. All changes of one patch are merged into a plain copy of the item and the final state is parsed once, so legal combined changes (e.g. excluding an entry and clearing its contribution) pass in any order while field and cross-field constraints still hold. The patched item's scientific references then run through the same checks as saved records — existence at the exact revision, record types, paper ownership, numeric fragment containment — so a patch cannot bypass the normal entry points. Patch application is provided by records.patches; transactional saving and recovery are responsibilities of the workspace and runtime interfaces.

- **Revision history**: Records of the same record_id must have strictly increasing unique revisions; existing revisions are never overwritten in place.

- **Material integrity**: When a materials root is supplied, package file hashes, external raw responses and evidence locators (text line ranges and excerpts, table cells, JSON pointers) are verified against the actual material; a changed hash or a mismatched excerpt is rejected. A locator whose selector kind has no verifier is reported as unverified and fails validation — unsupported locators never silently count as verified.

- **Release binding**: The manifest hash is recomputed from the actual release content (data version, content refs, artifacts, source runs, code version, change summary, channel targets) and must match the stored manifest_sha256. A non-null approval must bind the approver, time, the recomputed manifest hash and a declared channel target; a failed channel cannot be reported as succeeded and success requires a receipt reference.

## Filling formats and expansion rules

Step submissions contain only the scientific fields of that step. The application supplies fixed paper and source context, object identifiers, record identifiers, references, revisions and all execution metadata; none of these are model-filled. The common instruction text sent with every step is:

> Return only the scientific fields requested for this step. Write new summaries, questions and precedent notes in English; preserve the paper's printed identifiers, values, units, conditions and excerpts in their original form. Use only the supplied paper materials and fixed references. Provide every required list explicitly; an empty list does not establish that the paper reported nothing. Keep material gaps, unresolved scientific questions and the authors' assessment distinct. Supply the evidence required by this step; never invent missing content or silently resolve conflicting results. Use application-supplied identifiers where provided. The application creates persistent record identifiers, references and execution metadata; it does not supply scientific judgments or expert decisions.

Numeric filling rules:

- Always provided by the extractor: `limit_kind`, `condition`, `source`, `evidence`.
- Omittable when not applicable: `value`, `error`, `lower_error`, `upper_error`, `unit`, `range_lower`, `range_upper`, `lower_inclusive`, `upper_inclusive`, `interval_kind`, `coverage`, `lower_quantile`, `upper_quantile`, `error_components`, `frame`, `epoch`, `coordinate_format`, `representation`, `purpose`, `source_note`, `author_usage`. The application fills omitted fields with `null` (or an empty list for `error_components`); this only marks the field as not provided and implies no scientific conclusion.
- Reported single-sided errors, zeros, negative values, percentages and statistical interpretations must be preserved; a pure interval carries endpoints and an `interval_kind` but never a computed central value.
- Evidence is given inline as `{source, parts, context}`. The application validates each locator and excerpt, creates evidence records, assigns identifiers, and merges only evidence that is identical under the same fixed source version. Extractors never maintain evidence keys or persistent identifiers.
- Coverage is declared once per object as `{scope: all_requested, common, exceptions}` and expanded deterministically to one entry per requested quantity. Missing declarations, unknown or duplicate exceptions, unknown files, gaps or questions, and complete declarations that contradict known material gaps are rejected. Gaps whose impact scope is unknown block complete declarations for every quantity.
- Each explicit numeric conflict generates a question record of category `numeric_conflict` bound to the affected object; the application assigns identifiers and never answers or closes questions itself.
- A technical failure produces no success-shaped result: the failed step keeps its error records and previously saved valid results, and no empty measurement, roster or coverage entry is fabricated.
