<!-- Generated from hvs_catalog.records.catalog and catalogue.contracts. -->
# Fixed JSON catalog

The build command is offline. It reads exact extraction generations and optional frozen
SIMBAD responses. It never refreshes the service implicitly or modifies source workspaces.
Select the complete intended paper versions explicitly. Every build starts from these inputs;
no previous catalog, numbering, grouping or SIMBAD decision is an input.

## Commands

- `hvs-catalog catalog check-inputs --results LITERATURE_ROOT` verifies final results
  and their historical provenance without associating or exporting stellar objects.
- `hvs-catalog catalog select --results LITERATURE_ROOT --data-version VERSION --output SELECTION`
  freezes the current runs/<bare-arxiv-id>/results files for this build.
  This immutable build selection is not a separately maintained literature index.
- `hvs-catalog catalog select --index INDEX --space SPACE --data-version VERSION --output SELECTION`
  retains legacy execution-index compatibility without guessing latest results.
- `hvs-catalog catalog build --selection SELECTION --output NEW_DIRECTORY [--simbad RESPONSES]`
  publishes a new complete snapshot; an existing destination is never overwritten.
- `hvs-catalog catalog verify CATALOG` validates hashes, references, source coverage,
  and exact scientific values and evidence, without authenticating scientific correctness.
- `hvs-catalog catalog query-plan --selection SELECTION --output PLAN`
  prepares an explicit query list for review. The list includes identifier queries
  and possible coordinate fallbacks; remove queries outside the approved scope.
- `hvs-catalog catalog query --plan PLAN --output RESPONSES` explicitly contacts SIMBAD
  for only that list. Existing responses are reused, including failures.
  A refreshed response directory requires approval of the query scope. Rebuilding
  uses only the supplied frozen responses; missing responses remain not_queried.

## Final literature inputs

Each final results directory contains exactly literature_contribution.json,
evidence_audit.json and run.json. Old-style arXiv slashes become underscores in
directory names only; the original ID and version remain in scientific metadata.
Final run and audit references are relative to the delivery root. source.root is
also relative to that root; source.run_ref is relative to the historical workspace.
The final wrapper is not an execution and the delivery root needs no business database.
Only explicitly referenced historical runs are read. Historical scripts are never run.
Science bytes, paper/version, completion and audit observations must match the verified
source; only the audit result path is rebound. The private build selection binds the
final wrapper, historical run and delivered file hashes. Execution records are not published.
Cumulative repairs retain validated prior stages plus the selected later replacements.
Local container recoveries replay their saved receipts against exact archived responses,
preserve every scalar and accepted group, and pass the original scientific validators.
This read-only compatibility path does not extend the live response parser.
Missing files, changed hashes, escaping/symlink paths and inconsistent lineage are refused.

## Readable publication format (schema version 4)

manifest.json contains the data version, counts, build fingerprints and two hashed index references.
papers/index.json binds each versioned paper to its science, audit and PDF provenance.
Paper titles, authors and notes already exist in the science file and are not repeated in this index.
The optional incomplete field records only unfinished stages and retained scientific diagnostics.
An absent incomplete field means that the retained extraction is complete, not scientifically verified.
stars/index.json lists catalog IDs, display names and hashed files; fresh numbering starts at one.
Each stellar sources entry contains title, authors, pdf_url and original /stars/N record pointers.
The versioned source key identifies the exact paper; pointers address that paper's science file.
Paper-wide notes remain in papers. Execution errors, snapshots and repair-stage IDs stay in the workspace.
provenance/<catalog_id>_provenance.json delivers all associations, historical origins and the complete SIMBAD trace.
Each top-level stellar provenance reference binds that file by path and SHA-256; paths are catalog-root relative.
The complete catalog is portable and verifiable without the original build workspace or network.
The reader accepts versions 1, 2, 3 and 4 for verification.
Earlier version-four provenance files without the _provenance suffix remain readable.

## Scientific and identity rules

Each stellar JSON embeds all retained scientific values, evidence, and paper metadata.
Every identifier, contribution type, and value has a source key: bare arXiv ID plus version.
There is no contributions container and no duplicate complete-paper payload.
Numerical strings, units, conditions, uncertainty forms, and source-specific duplicates
are preserved. Paper-level notes retain their paper-wide scope in the original paper file.

Files use unique hvs-catalog-NNNNNN IDs. Display names are readable, nonunique labels.
Use the selected SIMBAD main_id for either identifier or coordinate matches.
Otherwise use the shortest paper identifier containing a letter, after collapsing whitespace.
Equal lengths prefer more distinct papers, then lexical order; no catalog-prefix whitelist applies.
Generic local number labels (Star, Object, Obj, Source, ID, No, Row, Candidate or Target)
append the bare arXiv ID, for example ID 7 (2601.00001). If several papers use the label,
choose the first bare arXiv ID in lexical order. Compare label lengths before appending it.
If no lettered identifier exists, use catalog_id. All original identifiers remain unchanged.
Unresolved SIMBAD candidates are not selected for naming; paper labels remain available.
Duplicate names are allowed. Neither names nor coordinate matches become identifier merge evidence.
Coordinate-match warnings remain. Names are recalculated from current inputs without inheritance.

Coordinates require a unique RA/Dec pair from one source object, linked by shared
evidence locations or identical nonempty conditions. Descriptions need not be identical.
Repeated identical coordinates share a query; different candidate values remain unused.
Common hms/dms labels, sexagesimal typography and separate central/error-unit labels
are parsed for queries only. All original scientific fields remain unchanged.
Frames come from coordinate conditions, then coordinate quotes, then explicit Gaia
coordinate provenance. Unlabelled modern equatorial coordinates may assume ICRS.
Explicit incompatible or unsupported frames are never overridden by that assumption.
Known frames may be transformed; coordinates are never propagated between epochs.
SIMBAD queries and association coordinate_interpretation(s) record
source nodes, original coordinate evidence, frame basis and whether ICRS was assumed.
An assumed frame also adds an English warning, including when no candidate is found.
Queries omit source provenance to retain reusable frozen response keys. Rules version 3
adds automatic source-evidence conflict checks and fresh construction after version 2's coordinate policy.
Rules version 4 changes display naming only; scientific identity and matching rules are unchanged.
The association radius is 5 arcseconds. Every union checks the entire group's Gaia
release identities, including all frozen representations of selected SIMBAD objects.
SIMBAD merge edges retain the candidate and query evidence used by this check.
Coordinate-only associations retain warnings.
SIMBAD coordinate searches select the nearest candidate, warn for multiple candidates,
and retain ambiguity for tied distances. Reliable conflicting identities block selection.

SIMBAD values remain separate from literature quantities. selected groups returned science into
coordinates, proper_motion, parallax, velocity, spectral_type and photometry, with units, errors,
quality and bibliographic references beside each quantity. Missing data are omitted, not derived.
No numerical value is rounded or converted. Coordinate and proper-motion error ellipses retain
major, minor and angle with their returned units. Proper motion ra_cosdec includes cos(dec).
velocity retains radial_velocity, redshift, reported_as and the original error with its own unit;
the two velocity representations are not independent measurements. Photometry keeps its band,
original system code (V=Vega, A=AB), value, error, quality and bibcode. A missing returned unit
remains absent; the column name flux does not imply a physical flux unit.
Nonempty unfamiliar fields remain in additional_fields. Empty strings, duplicate identity/position
columns, precision codes and database metadata are kept only in the original trace snapshot.
The stellar match summary lists successful identifiers or adopted coordinate source records,
distances, radii, multiple-candidate counts and assumed frames. Unresolved identities list candidate
names without selecting their science. last_queried_at is the latest primary query timestamp,
not a promise that every selected field was refreshed then. reason is omitted when absent.
Service errors are not no-match results. Transport XML remains frozen in the build workspace;
the delivered trace preserves all parsed candidate fields, individual queries and their response hashes.
Conflicts retain rejected candidates and query bindings in that delivered trace.
Incompatible external identities remain conflicts instead of selecting one input.

The stellar file keeps final source membership and warnings; all association details live in provenance.
The provenance associations list retains accepted and rejected links, including their exact evidence.
A shared identifier is recorded once with its source nodes, instead of all pairwise edges.
Coordinate and paper correspondences retain their actual endpoints and evidence.
Self-links, previous-build anchor links and duplicate associations are omitted.
Current endpoints resolve through sources and the fixed paper files. Superseded origins are
stored once in association_origins. Historical scientific associations retain their original versions;
their provenance never adds superseded measurements to current scientific values.
SIMBAD coordinate bindings live in the trace's simbad.bindings and retain the exact source node,
coordinate evidence, query/response binding, candidate distances, radius and nearest selection.
Within each trace, selected and distinct alternative frozen representations are stored once;
matches and association evidence use local {$ref: '#/simbad/...'} references. related_queries and
related_matches retain other or superseded snapshots used only by identity evidence.
SIMBAD identity links share the same associations list as identifier, coordinate and paper links.
Required historical origins are embedded once in association_origins. All local evidence references
resolve inside the unified provenance file. There is no separate SIMBAD-only trace in version 4.
Verification checks both hashes and the exact science/summary projection against the trace,
then restores the full evidence for identity and source-coverage checks.
References never replace literature measurements or their evidence. No numeric strings are rewritten.

warnings is the final field and contains deduplicated English notices about the current star.
Coordinate-only matching, assumed ICRS, multiple candidates, unresolved identity and failed
enrichment remain visible. Incomplete object extraction names the exact source record.
Quote-audit warnings name the source field, location and reason; whitespace-only differences
remain in the original audit without a stellar warning. A warning does not prove a scientific error.

Groups and conflict decisions precede numbering. IDs are assigned deterministically
from the complete selected source records, without old-ID protection or identity revisions.
The builder detects nearby same-paper records whose identifier evidence quotes contain
one explicitly row-bound sexagesimal coordinate pair each, with positions more than five
arcseconds apart and a source position agreeing with one row but contradicting the other.
Incomplete, ambiguous, unbound or image-only quotes do not establish this contradiction.
The conflicting pair remains separate under direct and transitive associations, including SIMBAD.
Original values and quotes are retained. A contradiction is not a corrected position or a PDF verification.

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
| `explicit_associations` | list of `ExplicitAssociation` | no |  |  |

#### CatalogRelease

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `schema_version` | `2` / `3` / `4` | no |  |  |
| `data_version` | `string` | yes |  |  |
| `counts` | `CatalogCounts` | yes |  |  |
| `papers` | `ArtifactRef` | yes |  |  |
| `stars` | `ArtifactRef` | yes |  |  |
| `rules_version` | `1` / `2` / `3` / `4` | yes |  |  |
| `software_version` | `string` | yes |  |  |
| `input_fingerprint` | `string` | yes | pattern `^[0-9a-f]{64}$` |  |
| `simbad_fingerprint` | `string` | yes | pattern `^[0-9a-f]{64}$` |  |
| `previous_manifest_sha256` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |

#### CatalogCounts

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `papers` | `integer` | yes | minimum 0 |  |
| `stars` | `integer` | yes | minimum 0 |  |
| `source_records` | `integer` | yes | minimum 0 |  |
| `values` | `integer` | yes | minimum 0 |  |

#### PaperIndex

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `schema_version` | `2` / `3` / `4` | no |  |  |
| `papers` | `object` | yes |  |  |

#### PaperEntry

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `science` | `ArtifactRef` | yes |  |  |
| `audit` | `ArtifactRef` | yes |  |  |
| `original_audit_sha256` | `string` | yes | pattern `^[0-9a-f]{64}$` |  |
| `pdf_url` | `string` | yes |  |  |
| `pdf_sha256` | `string` | yes | pattern `^[0-9a-f]{64}$` |  |
| `pdf_origin` | `object` | yes |  |  |
| `incomplete` | `object` | no |  | Only unfinished paper/record states and retained scientific diagnostics; absence means complete. |

#### StarIndex

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `schema_version` | `2` / `3` / `4` | no |  |  |
| `next_id` | `integer` | yes | minimum 1 |  |
| `retired_ids` | `object` | yes |  |  |
| `stars` | list of `StarEntry` | yes |  |  |

#### StarEntry

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `catalog_id` | `string` | yes | pattern `^hvs-catalog-[0-9]{6,}$` |  |
| `display_name` | `string` | yes |  |  |
| `file` | `ArtifactRef` | yes |  |  |

#### StellarRecord

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `data_version` | `string` | yes |  |  |
| `catalog_id` | `string` | yes | pattern `^hvs-catalog-[0-9]{6,}$` |  |
| `display_name` | `string` | yes |  |  |
| `sources` | `object` | yes |  |  |
| `identifiers` | list of `CatalogIdentifier` | yes | minItems 1 |  |
| `contribution_types` | list of `CatalogContributionType` | yes | minItems 1 |  |
| `quantities` | list of `CatalogQuantity` | yes |  |  |
| `warnings` | list of `string` | yes |  |  |
| `schema_version` | `4` | no |  |  |
| `simbad` | `SimbadSummary` | yes |  |  |
| `provenance` | `ArtifactRef` | yes |  | Complete identity and SIMBAD evidence, relative to the catalog root and bound by SHA-256. |

#### StarSource

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `title` | `string` | yes |  |  |
| `authors` | list of `string` | yes |  |  |
| `pdf_url` | `string` | yes |  |  |
| `records` | list of `string` | yes | minItems 1 | Original object pointers in this exact paper version's science file. |

#### IdentifierAssociation

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `method` | `identifier` | no |  |  |
| `identifier` | `string` | yes |  |  |
| `nodes` | list of `string` | yes | minItems 2 |  |

#### IdentityLink

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `method` | `identifier` / `coordinates` / `paper` / `simbad` | yes |  |  |
| `left` | `string` | yes |  |  |
| `right` | `string` | yes |  |  |
| `accepted` | `boolean` | no |  |  |
| `historical` | `boolean` | no |  |  |
| `details` | `object` | yes |  |  |
| `origins` | `object` | no |  | Local references only for origins absent from the current paper files. |

#### SimbadSummary

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `status` | `not_queried` / `matched` / `not_found` / `ambiguous` / `conflict` / `failed` | yes |  |  |
| `method` | `identifier` / `coordinates` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `selected` | `SimbadScience` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `reason` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `match` | `SimbadMatchSummary` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `last_queried_at` | `string` \| `null` | no |  | Latest recorded primary query time; individual times remain in the trace. Nullable: absent content is `null`, never a default value. |

#### SimbadScience

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `main_id` | `string` | yes |  |  |
| `aliases` | list of `string` | no |  |  |
| `object_type` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `coordinates` | `SimbadCoordinates` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `proper_motion` | `SimbadProperMotion` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `parallax` | `SimbadMeasurement` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `velocity` | `SimbadVelocity` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `spectral_type` | `SimbadMeasurement` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `photometry` | list of `SimbadPhotometry` | no |  |  |
| `additional_fields` | `object` | no |  | Other nonempty returned fields; unfamiliar scientific columns are never discarded. |

#### SimbadValue

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `value` | `JsonValue` | no |  |  |
| `unit` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |

#### SimbadMeasurement

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `value` | `JsonValue` | no |  |  |
| `unit` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `error` | `JsonValue` | no |  |  |
| `error_unit` | `string` \| `null` | no |  | Present only when the reported error unit differs. Nullable: absent content is `null`, never a default value. |
| `quality` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `bibcode` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `wavelength` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |

#### SimbadCoordinates

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `ra` | `SimbadValue` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `dec` | `SimbadValue` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `frame` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `equinox` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `error_ellipse` | `SimbadEllipse` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `quality` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `bibcode` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `wavelength` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |

#### SimbadProperMotion

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `ra_cosdec` | `SimbadValue` \| `null` | no |  | Right-ascension proper motion including cos(dec). Nullable: absent content is `null`, never a default value. |
| `dec` | `SimbadValue` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `error_ellipse` | `SimbadEllipse` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `quality` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `bibcode` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `wavelength` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |

#### SimbadEllipse

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `major` | `SimbadValue` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `minor` | `SimbadValue` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `angle` | `SimbadValue` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |

#### SimbadVelocity

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `radial_velocity` | `SimbadValue` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `redshift` | `SimbadValue` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `reported_as` | `string` \| `null` | no |  | Original SIMBAD rvz_type, not an inferred conversion. Nullable: absent content is `null`, never a default value. |
| `error` | `SimbadValue` \| `null` | no |  | Original rvz_err and its own returned unit. Nullable: absent content is `null`, never a default value. |
| `nature` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `quality` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `bibcode` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `wavelength` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |

#### SimbadPhotometry

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `value` | `JsonValue` | no |  |  |
| `unit` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `error` | `JsonValue` | no |  |  |
| `error_unit` | `string` \| `null` | no |  | Present only when the reported error unit differs. Nullable: absent content is `null`, never a default value. |
| `quality` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `bibcode` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `wavelength` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `band` | `string` | yes |  |  |
| `system` | `string` \| `null` | no |  | Original system code: V is Vega, A is AB. Nullable: absent content is `null`, never a default value. |
| `additional_fields` | `object` | no |  |  |

#### SimbadMatchSummary

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `identifiers` | list of `string` | no |  |  |
| `coordinates` | list of `SimbadCoordinateMatch` | no |  |  |
| `candidates` | list of `string` | no |  | Distinct object names only for unresolved identity. |

#### SimbadCoordinateMatch

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `source_record` | `string` | yes |  |  |
| `separation_arcsec` | `number` | yes |  |  |
| `radius_arcsec` | `number` | yes |  |  |
| `candidate_count` | `integer` \| `null` | no |  | Present when more than one candidate was considered. Nullable: absent content is `null`, never a default value. |
| `assumed_frame` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `historical` | `boolean` | no |  |  |

#### StellarProvenance

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `schema_version` | `4` | no |  |  |
| `data_version` | `string` | yes |  |  |
| `catalog_id` | `string` | yes | pattern `^hvs-catalog-[0-9]{6,}$` |  |
| `associations` | list of `IdentifierAssociation` \| `IdentityLink` | yes |  |  |
| `association_origins` | `object` | no |  | Once-only identity evidence for superseded source versions or contents. |
| `simbad` | `PublishedSimbad` | yes |  |  |

#### PublishedSimbad

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `status` | `not_queried` / `matched` / `not_found` / `ambiguous` / `conflict` / `failed` | yes |  |  |
| `method` | `identifier` / `coordinates` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `selected` | `SimbadCandidate` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `reason` | `string` \| `null` | no |  | Nullable: absent content is `null`, never a default value. |
| `queries` | list of `object` | no |  |  |
| `related_queries` | list of `object` | no |  | Queries from other or superseded snapshots, used only by association evidence. |
| `matches` | list of `object` | no |  | Query matches reference selected or alternatives instead of repeating data. |
| `related_matches` | list of `object` | no |  |  |
| `alternatives` | list of `SimbadCandidate` | no |  | Distinct frozen candidate representations required by matching evidence. |
| `bindings` | list of `IdentityLink` | no |  | Adopted coordinate matches with exact source, query, distance and selection. |

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
