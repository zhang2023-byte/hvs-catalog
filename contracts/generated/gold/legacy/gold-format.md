<!-- Generated -->
# Gold v1 working annotation format (compatibility)

This compatibility document is generated from the typed definitions in `hvs_catalog.evaluation.models` for `evaluation.hvs_contribution_gold` v1 and the legacy v4 reader in `hvs_catalog.evaluation.legacy`. Do not edit it by hand; regenerate with `uv run python tools/render_record_contracts.py`.

Current formal Gold uses the answer v2 and metadata v3 contract in [current Gold format](../gold-format.md). This v1 format remains readable for migration comparison and previously saved synthetic/historical records.

## Scope

The v1 working annotation captures expert judgments with PDF evidence; it shares the science vocabulary with prediction records but does not carry run fields (execution ids, framework progress, or automated extraction methods). A document is either a `draft` (may miss information required of a formal answer; explicit `pending` items and check results describe gaps) or an `answer` (its `pending` array is empty; historical v1 saving required format checks and expert approval). Explicitly accepted scientific pending items remain in the saved provenance and approval in the v1 layout; an empty answer array does not mean all scientific questions have been resolved.

Evidence keeps the expert's PDF discipline: a registered file with its content hash, a verbatim location description, an optional 1-based PDF page ordinal (a position in the file, distinct from a printed page number), and an optional verbatim quote. Page ordinals are never guessed from the location text.

## Vocabulary

Quantities reuse the 18 approved science rule paths at rules version `0.5.0`; the author overall assessment states are the current rule states (`no_overall_conclusion`, `unbound`, `possibly_unbound`, `possibly_bound`, `bound`). The legacy status vocabulary is preserved verbatim on migration via `legacy_status` (`unbound`, `possibly_unbound`, `bound`, `no_overall_conclusion`, `not_assessed`); `not_assessed` no longer exists in the current rules and is reported for expert review instead of being converted.

| Vocabulary | Values |
|---|---|
| limit kinds | `none`, `lower_limit`, `upper_limit`, `range` |
| interval kinds | `ordinary_range`, `prior_support`, `confidence`, `credible`, `statistical_interval`, `unspecified` |
| representations | `linear`, `log10`, `distance_modulus` |
| coordinate formats | `decimal_degrees`, `sexagesimal_hms`, `sexagesimal_dms`, `sexagesimal_colon` |

## Fields

#### `GoldAnnotation`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `schema` | GoldSchemaTag | required | — | |
| `kind` | answer \| draft | required | — | |
| `document_id` | string | required | — |min length 1 |
| `paper` | GoldPaper | required | — | |
| `rules` | GoldRulesRef | required | — | |
| `files` | array of GoldPdfFile | required | — | |
| `status` | contributions_found \| no_contributions | required | — | |
| `contributions` | array of GoldContribution | required | — | |
| `reviewed_exclusions` | array of GoldExclusion | required | — | |
| `provenance` | GoldProvenance | required | — | |
| `material_correspondence` | GoldMaterialCorrespondence | optional | {"status": "pending", "pdf_file_ids": [], "checked_by": null, "checked_at": null, "basis": "", "differences": []} | |
| `pending` | array of GoldPendingItem | optional | [] | |

### Approval

#### `SaveApproval`

Human declaration for this exact binding, not metadata review alone.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `approved_by` | string | required | — |min length 1 |
| `approver_kind` | human | required | — | |
| `approved_at` | AwareDatetime | required | — | |
| `binding` | SaveBinding | required | — | |
| `binding_sha256` | string | required | — | |
| `basis` | string | required | — |min length 1 |
| `scientific_review` | boolean | optional | false | |
| `accepted_pending` | array of string | optional | [] | |

### Maker identity

#### `GoldActor`

Declared maker identity; AI identity never constitutes expert approval.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `actor_id` | string | required | — |min length 1; pattern `\S` |
| `kind` | ai \| human | required | — | |
| `model` | null \| string | optional | null | |

### Human expert

#### `GoldHuman`

Declared human expert identity; no authentication is implied.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `actor_id` | string | required | — |min length 1; pattern `\S` |
| `kind` | human | optional | "human" | |

### Original human expert

#### `GoldOriginalExpert`

Historical human origin, confirmed from a cited original record.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `expert` | GoldHuman \| null | optional | null | |
| `annotated_at` | AwareDatetime \| null | optional | null | |
| `basis` | string | required | — |min length 1; pattern `\S` |
| `unknown_reason` | null \| string | optional | null | |

### Draft authorship

#### `GoldPreparationAuthor`

Who prepared this particular draft or revision, and why.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `actor` | GoldActor | required | — | |
| `prepared_at` | AwareDatetime | required | — | |
| `action` | initial_draft \| migration \| revision | required | — | |
| `description` | string | required | — |min length 1; pattern `\S` |

### Material correspondence

#### `GoldMaterialCorrespondence`

Expert-declared fixed PDF scope; hashing does not prove scientific correctness.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `status` | checked \| pending | optional | "pending" | |
| `pdf_file_ids` | array of string | optional | [] | |
| `checked_by` | GoldHuman \| null | optional | null | |
| `checked_at` | AwareDatetime \| null | optional | null | |
| `basis` | string | optional | "" | |
| `differences` | array of string | optional | [] | |

### Provenance

#### `GoldProvenance`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `origin` | migration \| new_annotation | required | — | |
| `legacy` | GoldLegacySource \| null | optional | null | |
| `note` | string | optional | "" | |
| `original_expert` | GoldOriginalExpert \| null | optional | null | |
| `original_approval_version` | integer \| null | optional | null | |
| `prepared_by` | GoldPreparationAuthor \| null | optional | null | |

### Legacy source

#### `GoldLegacySource`

Migration provenance preserves original legacy annotation metadata verbatim without
constituting new approval.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `schema_name` | string | required | — |min length 1 |
| `schema_version` | integer | required | — |≥ 1 |
| `file_name` | string | required | — |min length 1 |
| `sha256` | string | required | — |pattern `^[0-9a-f]{64}$` |
| `arxiv_id` | string | required | — |min length 1 |
| `annotator` | string | required | — |min length 1 |
| `annotated_at` | string | required | — |min length 1 |
| `guideline_version` | string | required | — |min length 1 |
| `annotation_process` | dict | optional | {} | |
| `canary` | string | optional | "" | |

### Pending item

#### `GoldPendingItem`

Explicit unresolved draft items, forbidden in official answers.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `item_id` | string | required | — |min length 1 |
| `category` | missing_info \| science_review \| structural_gap | required | — | |
| `subject` | string | required | — |min length 1 |
| `description` | string | required | — |min length 1 |

### Paper

#### `GoldPaper`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `paper_key` | string | required | — |min length 1 |
| `arxiv_id` | null \| string | required | — | |
| `version` | null \| string | optional | null | |

### Rules reference

#### `GoldRulesRef`

Scientific rules used for the original annotation; current scoring checks compatibility.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `version` | string | required | — |min length 1 |
| `sha256` | string | required | — |pattern `^[0-9a-f]{64}$` |

### PDF file

#### `GoldPdfFile`

One fixed PDF material; an unverified hash stays pending rather than fabricated.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `file_id` | string | required | — |min length 1 |
| `name` | string | required | — |min length 1 |
| `sha256` | null \| string | optional | null | |

### Evidence

#### `GoldEvidence`

Location where an expert saw the content in the fixed PDF.

Preserve location descriptions (page/table/section/object row) verbatim. page is the
one-based file page ordinal, distinct from printed page numbers; fill only when
explicitly known, never inferred from text. quote is optional; an empty string means
absent.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `file_id` | null \| string | optional | null | |
| `location` | string | required | — |min length 1 |
| `page` | integer \| null | optional | null | |
| `quote` | string | optional | "" | |

### Contribution

#### `GoldContribution`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `contribution_id` | string | required | — |min length 1 |
| `legacy_index` | integer \| null | optional | null | |
| `identifiers` | array of GoldIdentifier | required | — |≥ 1 items |
| `contribution_type` | candidates_found \| follow_up | required | — | |
| `contribution_summary` | string | required | — |min length 1 |
| `contribution_evidence` | array of GoldEvidence | required | — | |
| `paper_boundness` | GoldBoundness | required | — | |
| `quantities` | array of GoldQuantityGroup | required | — | |
| `quantity_conflicts` | array of GoldConflict | required | — | |

### Identifier

#### `GoldIdentifier`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `value` | string | required | — |min length 1 |
| `evidence` | array of GoldEvidence | required | — | |

### Boundness

#### `GoldBoundness`

Overall author state: unresolved new vocabulary state is null; preserve legacy state
verbatim for checking.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `status` | null \| string | optional | null | |
| `legacy_status` | null \| string | optional | null | |
| `evidence` | array of GoldEvidence | required | — | |

### Quantity group

#### `GoldQuantityGroup`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `quantity` | string | required | — |min length 1 |
| `values` | array of GoldValue | required | — |≥ 1 items |

### Value

#### `GoldValue`

Gold representation of scientific values; absent contents are null and drafts permit
empty evidence.

Follow shared MeasuredValue rules: allow one-sided asymmetric errors and
independently evidenced total/named components together, but never mix
symmetric/asymmetric forms. Represent empty strings as absent null values.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `value` | null \| string | required | — | |
| `error` | null \| string | required | — | |
| `lower_error` | null \| string | required | — | |
| `upper_error` | null \| string | required | — | |
| `unit` | null \| string | required | — | |
| `limit_kind` | lower_limit \| none \| range \| upper_limit | optional | "none" | |
| `range_lower` | null \| string | required | — | |
| `range_upper` | null \| string | required | — | |
| `coordinate_format` | decimal_degrees \| sexagesimal_colon \| sexagesimal_dms \| sexagesimal_hms \| null | optional | null | |
| `condition` | null \| string | required | — | |
| `interval_kind` | confidence \| credible \| ordinary_range \| prior_support \| statistical_interval \| unspecified \| null | optional | null | |
| `coverage` | GoldProbability \| null | optional | null | |
| `lower_quantile` | GoldProbability \| null | optional | null | |
| `upper_quantile` | GoldProbability \| null | optional | null | |
| `error_components` | array of GoldErrorComponent | required | — | |
| `representation` | distance_modulus \| linear \| log10 \| null | optional | null | |
| `source` | prior_work \| this_paper \| unclear | required | — | |
| `source_note` | null \| string | required | — | |
| `evidence` | array of GoldEvidence | required | — | |
| `context_evidence` | array of GoldEvidence | required | — | |

### Probability

#### `GoldProbability`

Explicitly printed coverage probability or quantile; consumers perform conversions
without writing them back.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `value` | string | required | — |min length 1 |
| `unit` | % \| fraction | required | — | |
| `evidence` | array of GoldEvidence | required | — |≥ 1 items |

### Error component

#### `GoldErrorComponent`

One named original error component, without inferred total errors.

Follow shared ErrorComponent rules: one-sided asymmetric errors are allowed,
symmetric/asymmetric forms cannot mix, and at least one error value is required.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `name` | string | required | — |min length 1 |
| `error` | null \| string | required | — | |
| `lower_error` | null \| string | required | — | |
| `upper_error` | null \| string | required | — | |
| `evidence` | array of GoldEvidence | required | — |≥ 1 items |

### Conflict

#### `GoldConflict`

Unresolved competing evidence; complete alternative value structures are optional,
with missing contents reported for completion.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `conflict_id` | string | required | — |min length 1 |
| `quantity` | string | required | — |min length 1 |
| `reason` | string | required | — |min length 1 |
| `evidence` | array of GoldEvidence | required | — |≥ 2 items |
| `alternatives` | array of GoldValue | required | — | |

### Exclusion

#### `GoldExclusion`

Paper-level exclusions; missing independent object IDs in legacy structures remain
pending correspondence items.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `exclusion_id` | string | required | — |min length 1 |
| `reason` | string | required | — |min length 1 |
| `evidence` | array of GoldEvidence | required | — |≥ 1 items |

## Legacy reader scope

The reader accepts `benchmark.hvs_contribution_annotation` v4 only; older versions are rejected explicitly and never silently upgraded. Its vocabulary: contribution types candidates_found, follow_up, limit kinds none, lower_limit, upper_limit, range, interval kinds ordinary_range, prior_support, confidence, credible, statistical_interval, representations linear, log10, distance_modulus, coordinate formats decimal_degrees, sexagesimal_hms, sexagesimal_dms, sexagesimal_colon.
