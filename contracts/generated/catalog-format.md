<!-- Generated from hvs_catalog.records.catalog and catalogue.contracts. -->
# Fixed JSON catalog

The build command is offline. It reads exact extraction generations and optional frozen
SIMBAD responses. It never refreshes the service implicitly or modifies source workspaces.
Select the complete intended paper versions explicitly; previous papers are retained
unless the selection provides a replacement for the same bare arXiv ID.

## Commands

- `hvs-catalog catalog check-inputs --results LITERATURE_ROOT` verifies final results
  and their historical provenance without associating or exporting stellar objects.
- `hvs-catalog catalog select --results LITERATURE_ROOT --data-version VERSION --output SELECTION`
  freezes the current runs/<bare-arxiv-id>/results files for this build.
  This immutable build selection is not a separately maintained literature index.
- `hvs-catalog catalog select --index INDEX --space SPACE --data-version VERSION --output SELECTION`
  retains legacy execution-index compatibility without guessing latest results.
- `hvs-catalog catalog build --selection SELECTION --output NEW_DIRECTORY [--previous CATALOG] [--simbad RESPONSES]`
  publishes a new complete snapshot; an existing destination is never overwritten.
- `hvs-catalog catalog verify CATALOG` validates hashes, references, source coverage,
  and exact scientific values and evidence, without authenticating scientific correctness.
- `hvs-catalog catalog query-plan --selection SELECTION --output PLAN [--previous CATALOG]`
  prepares an explicit query list for review. The list includes identifier queries
  and possible coordinate fallbacks; remove queries outside the approved scope.
- `hvs-catalog catalog query --plan PLAN --output RESPONSES` explicitly contacts SIMBAD
  for only that list. Existing responses are reused, including failures.
  To refresh, list existing object IDs in selection.simbad_refresh and use a new
  response directory after approving its scope. Retirement keeps the original IDs
  for refresh selection; refreshing one member queries its entire merged group.

## Final literature inputs

Each final results directory contains exactly literature_contribution.json,
evidence_audit.json and run.json. Old-style arXiv slashes become underscores in
directory names only; the original ID and version remain in scientific metadata.
Final run and audit references are relative to the delivery root. source.root is
also relative to that root; source.run_ref is relative to the historical workspace.
The final wrapper is not an execution and the delivery root needs no business database.
Only explicitly referenced historical runs are read. Historical scripts are never run.
Science bytes, paper/version, completion and audit observations must match the verified
source; only the audit result path is rebound. CatalogSource.run_sha256 binds the selected
final wrapper, which in turn binds the exact historical run and delivered file hashes.
Cumulative repairs retain validated prior stages plus the selected later replacements.
Local container recoveries replay their saved receipts against exact archived responses,
preserve every scalar and accepted group, and pass the original scientific validators.
This read-only compatibility path does not extend the live response parser.
Missing files, changed hashes, escaping/symlink paths and inconsistent lineage are refused.

## Scientific and identity rules

Each stellar JSON embeds all retained scientific values, evidence, and paper metadata.
Every identifier, contribution type, and value has a source key: bare arXiv ID plus version.
There is no contributions container and no duplicate complete-paper payload.
Numerical strings, units, conditions, uncertainty forms, and source-specific duplicates
are preserved. Paper-level notes retain their paper-wide scope.

Files use stable hvs-catalog-NNNNNN IDs. Display names prefer reliable SIMBAD main IDs,
then complete Gaia DR3, EDR3, DR2, DR1 IDs, then other recognized complete catalog IDs
by distinct-paper count and lexical ties, and finally the internal catalog ID.
Complete prefixes, releases and components are retained. Valid existing display names
remain stable unless a higher-priority identifier or correction requires a change.
Reliable prior SIMBAD main IDs and aliases are reusable fixed identity evidence for
new papers and replacement versions, including during explicit enrichment refresh.
Ambiguous prior alias links remain rejected, with their candidate/query evidence.
The affected current source group keeps conflict status and its catalog ID as name;
old objects that are only rejected link targets retain their own enrichment state.
Coordinate-only SIMBAD names cannot promote display names or identifier associations.
Name collisions fall back to separate catalog IDs and never imply an automatic merge.
Unresolved SIMBAD identity conflicts also use the internal catalog ID as display name.

Coordinates must have one paired RA/Dec value, explicit compatible conditions,
angular units and a recognized frame (ICRS, J2000/FK5, B1950/FK4).
Known frames may be transformed; coordinates are never propagated between epochs.
Unclear or multiple pairs remain unused for matching; original values are retained.
The association radius is 5 arcseconds. Every union checks the entire group's Gaia
release identities, including all frozen representations of selected SIMBAD objects.
SIMBAD merge edges retain the candidate and query evidence used by this check.
Coordinate-only associations retain warnings.
SIMBAD coordinate searches select the nearest candidate, warn for multiple candidates,
and retain ambiguity for tied distances. Reliable conflicting identities block selection.

SIMBAD values remain separate from literature quantities. Basic returned columns
preserve their units and available uncertainty and bibliographic columns; photometry
includes its band. Missing data are not derived. Service errors are not no-match results.
Raw service responses and query times are frozen outside the public catalog.
Conflicts retain rejected candidates and query bindings inside the stellar snapshot.
Explicit retirement merges preserve every original object's frozen snapshot evidence;
incompatible external identities remain conflicts instead of selecting one input.

Each association embeds identity-only origins for its paper endpoints, including
fixed metadata, result/PDF hashes and original identifiers with evidence.
Historical associations remain marked historical and retain their original versions;
their provenance never adds superseded measurements to current scientific values.
Coordinate SIMBAD associations include the exact source node, coordinate evidence,
query/response binding, candidate distances, radius and adopted nearest selection.

Existing object mergers and splits require explicit identity_assignments and retired_ids
in the selection. These mappings record a maintainer's identity correction, not an
automatic inference. Retired numbers are never reused.

Complete fictional record: [catalog_star.json](examples/catalog_star.json).
The example uses placeholder hashes and fabricated papers; it is not catalog data.

## Fields

#### FinalLiteratureResult

The selected paper result and audit, stored in runs/<bare ID>/results/run.json.

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `format` | `hvs.final-literature-result` | yes |  |  |
| `version` | `1` | yes |  |  |
| `arxiv_id` | `string` | yes |  |  |
| `arxiv_version` | `string` | yes |  |  |
| `completion` | `complete` / `partial` | yes |  |  |
| `result_ref` | `ArtifactRef` | yes |  | Final scientific file, relative to the delivery root. |
| `evidence_audit_ref` | `ArtifactRef` | yes |  | Same-generation audit, relative to the delivery root. |
| `source` | `FinalResultSource` | yes |  |  |

#### FinalResultSource

The exact historical run behind a final delivery; not a new extraction.

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `root` | `string` | yes | minLength 1 | Historical workspace root, relative to the delivery root. |
| `run_ref` | `ArtifactRef` | yes |  | Original extraction or repair run, relative to the historical root. |

#### CatalogSelection

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `schema_version` | `1` | no |  |  |
| `data_version` | `string` | yes | minLength 1 |  |
| `papers` | list of `PaperSelection` | yes |  |  |
| `simbad_refresh` | list of `string` | no |  | Existing catalog IDs whose external snapshots may be explicitly refreshed. |
| `explicit_associations` | list of `ExplicitAssociation` | no |  |  |
| `identity_assignments` | `object` | no |  | Explicit source-node to catalog ID assignments for identity revisions. |
| `retired_ids` | `object` | no |  | Explicit old to successor ID mappings; retired IDs are never reused. |

#### CatalogManifest

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `schema_version` | `1` | no |  |  |
| `data_version` | `string` | yes |  |  |
| `rules_version` | `1` | no |  |  |
| `software_version` | `string` | yes |  |  |
| `input_fingerprint` | `string` | yes |  |  |
| `simbad_fingerprint` | `string` | yes |  |  |
| `previous_manifest_sha256` | `string` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `papers` | `object` | yes |  |  |
| `stars` | list of `PublishedStar` | yes |  |  |
| `next_id` | `integer` | yes | minimum 1 |  |
| `retired_ids` | `object` | yes |  |  |

#### PublishedPaper

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `source` | `CatalogSource` | yes |  |  |
| `science` | `ArtifactRef` | yes |  |  |
| `audit` | `ArtifactRef` | yes |  |  |
| `original_audit_sha256` | `string` | yes |  |  |

#### PublishedStar

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `catalog_id` | `string` | yes | pattern `^hvs-catalog-[0-9]{6,}$` |  |
| `display_name` | `string` | yes |  |  |
| `file` | `ArtifactRef` | yes |  |  |
| `nodes` | list of `string` | yes |  |  |

#### CatalogStar

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `schema_version` | `1` | no |  |  |
| `data_version` | `string` | yes |  |  |
| `catalog_id` | `string` | yes | pattern `^hvs-catalog-[0-9]{6,}$` |  |
| `display_name` | `string` | yes |  |  |
| `sources` | `object` | yes |  |  |
| `identifiers` | list of `CatalogIdentifier` | yes | minItems 1 |  |
| `contribution_types` | list of `CatalogContributionType` | yes | minItems 1 |  |
| `quantities` | list of `CatalogQuantity` | yes |  |  |
| `simbad` | `SimbadSnapshot` | yes |  |  |
| `associations` | list of `Association` | yes |  |  |
| `warnings` | list of `string` | yes |  |  |

#### CatalogSource

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `arxiv_id` | `string` | yes | pattern `^(?:[0-9]{4}\.[0-9]{4,5}\|[a-zA-Z-]+(?:\.[A-Z]{2})?/[0-9]{7})$` | Bare arXiv identifier, without a version suffix. |
| `version` | `string` | yes | pattern `^v[1-9][0-9]*$` | arXiv paper version, such as v1; not a format version. |
| `title` | `string` | yes | minLength 1 | Paper title. |
| `authors` | list of `string` | yes |  | Paper authors in reported order. |
| `paper_notes` | list of `string` | yes |  |  |
| `result_status` | `complete` / `partial` | yes |  |  |
| `roster_status` | `complete` / `partial` / `failed` | yes |  |  |
| `roster_issues` | list of `object` | yes |  |  |
| `roster_rejected` | `object` | yes |  |  |
| `errors` | list of `object` | yes |  |  |
| `result_sha256` | `string` | yes |  |  |
| `run_sha256` | `string` | yes |  |  |
| `input_sha256` | `string` | yes |  |  |
| `pdf_sha256` | `string` | yes |  |  |
| `pdf_url` | `string` | yes |  | Versioned arXiv reference URL; not a claim about the download method. |
| `pdf_origin` | `object` | yes |  | Original source-package provenance, including synthetic or local import origins. |
| `objects` | list of `SourceObject` | yes |  |  |

#### SourceObject

A retained source object and its extraction completeness and diagnostics.

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `result_index` | `integer` | yes | minimum 0 |  |
| `source_index` | `integer` | yes | minimum 0 |  |
| `status` | `complete` / `partial` / `failed` | yes |  |  |
| `snapshot_ref` | `RecordRef` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `repair_stage_id` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `issues` | list of `object` | yes |  |  |
| `rejected` | `object` | yes |  |  |
| `source_paths` | list of `string` | yes |  |  |
| `evidence_audit` | list of `QuoteMismatch` | yes |  |  |

#### CatalogIdentifier

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `value` | `string` | yes | minLength 1 | Star identifier. |
| `evidence` | list of `TextEvidence` \| `ImageEvidence` | yes | minItems 1 | Evidence for this identifier. |
| `source` | `string` | yes |  |  |

#### CatalogContributionType

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `value` | `candidates_found` / `follow_up` | yes |  | Contribution category. Scientific inclusion rules are separate. |
| `evidence` | list of `TextEvidence` \| `ImageEvidence` | yes | minItems 1 | Evidence for this contribution type. |
| `source` | `string` | yes |  |  |

#### CatalogQuantity

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `quantity` | `ra` / `dec` / `distance` / `parallax` / `proper_motion_ra` / `proper_motion_dec` / `radial_velocity` / `galactocentric_radius` / `galactic_rest_frame_velocity` / `bound_probability` / `unbound_probability` | yes |  |  |
| `values` | list of `CatalogValue` | yes | minItems 1 |  |

#### CatalogValue

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `value` | `string` | no | minLength 1 | Reported point value. |
| `error` | `string` | no | minLength 1 | Reported symmetric error. |
| `lower_error` | `string` | no | minLength 1 | Reported lower error, preserving its original sign. |
| `upper_error` | `string` | no | minLength 1 | Reported upper error, preserving its original sign. |
| `lower_bound` | `string` | no | minLength 1 | Reported lower limit or interval endpoint. |
| `upper_bound` | `string` | no | minLength 1 | Reported upper limit or interval endpoint. |
| `lower_inclusive` | `boolean` | no |  | Whether the reported lower bound is included. |
| `upper_inclusive` | `boolean` | no |  | Whether the reported upper bound is included. |
| `unit` | `string` | no | minLength 1 | Unit, when applicable. |
| `condition` | `string` | no | minLength 1 | Relevant method, model, reference frame, or statistical meaning. |
| `evidence` | list of `TextEvidence` \| `ImageEvidence` | yes | minItems 1 | Evidence for this value and its context. |
| `source` | `string` | yes |  |  |

#### Association

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `left` | `string` | yes |  |  |
| `right` | `string` | yes |  |  |
| `method` | `identifier` / `coordinates` / `paper` / `simbad` / `previous` | yes |  |  |
| `accepted` | `boolean` | yes |  |  |
| `details` | `object` | yes |  |  |
| `historical` | `boolean` | no |  | True for an association adopted or considered in a previous snapshot. |
| `origins` | `object` | no |  | Identity-only provenance for every paper endpoint, including old versions. |

#### AssociationOrigin

Fixed paper identity evidence, including sources superseded by later versions.

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `source` | `LiteratureMetadata` | yes |  |  |
| `result_sha256` | `string` | yes | pattern `^[0-9a-f]{64}$` |  |
| `pdf_sha256` | `string` | yes | pattern `^[0-9a-f]{64}$` |  |
| `pdf_url` | `string` | yes |  |  |
| `pdf_origin` | `object` | yes |  |  |
| `result_index` | `integer` | yes | minimum 0 |  |
| `identifiers` | list of `LiteratureIdentifier` | yes | minItems 1 |  |

#### SimbadSnapshot

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `status` | `not_queried` / `matched` / `not_found` / `ambiguous` / `conflict` / `failed` | yes |  |  |
| `method` | `identifier` / `coordinates` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `selected` | `SimbadCandidate` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `queries` | list of `object` | no |  |  |
| `candidates` | list of `object` | no |  |  |
| `reason` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `warnings` | list of `string` | no |  |  |

#### SimbadQuery

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `method` | `identifier` / `coordinates` | yes |  |  |
| `identifier` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `position` | `SkyPosition` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `radius_arcsec` | `5.0` | no |  |  |

#### SimbadCandidate

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `main_id` | `string` | yes | minLength 1 |  |
| `aliases` | list of `string` | yes |  |  |
| `position` | `SkyPosition` \| `null` | yes |  | Nullable: absent content is `null`, never a default value. |
| `fields` | `object` | yes |  | Basic returned fields, including units, errors and references when available. |
| `photometry` | list of `object` | yes |  | Returned photometry with explicit band, errors and references when available. |

#### SimbadResponse

A frozen response kept in the private build workspace, not in the catalog.

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `query` | `SimbadQuery` | yes |  |  |
| `queried_at` | `string` | yes |  |  |
| `client_version` | `string` | yes |  |  |
| `service_version` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `candidates` | list of `SimbadCandidate` | yes |  |  |
| `raw_response` | `string` | yes |  |  |
| `error` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
