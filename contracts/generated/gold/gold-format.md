<!-- Generated -->
# Formal Gold format

The formal scoring reference is `answer.json` together with `metadata.json`. The answer contains scientific targets, PDF evidence, explicit coverage and remaining uncertainty. Metadata binds that exact answer to the paper version, fixed PDF hashes, scientific rules and concise attribution. Optional empty fields are omitted; an empty `contributions` array remains explicit.

`contribution_id` has the form `arxiv_id:candNN`; it is a generated identifier and needs no paper citation. Scientific claims use `value` and their corresponding `evidence`. The old v1 draft, migration indices, old boundness status, pending tasks and full historical provenance stay outside the formal pair.

Material and scope confirmation in metadata is distinct from final Gold approval. New answers and revisions require `scientific_review=true` from a human approver bound to the exact pair, target and predecessor. The current `approval.json` is stored alongside the pair; no preparation or operation log is kept. Revisions record preparer, human decision author, date, basis and the previous answer hash in `revision`. Authorized Git commits retain history; uncommitted copies do not.

See [version meanings](README.md#what-the-version-numbers-mean) for the distinction between format, paper, rules and saved Gold revisions.

## Top-level typed fields

#### `CompactGoldAnswer`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `arxiv_id` | string | required | — |pattern `^\d{4}\.\d{4,5}$` |
| `status` | contributions_found \| no_contributions | required | — | |
| `coverage` | CompactCoverage | required | — | |
| `contributions` | array of CompactContribution | required | — | |
| `reviewed_exclusions` | array of CompactExclusion | optional | [] | |
| `unresolved` | array of CompactUnresolved | optional | [] | |

#### `CompactGoldMetadata`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `schema` | CompactSchemaTag | required | — | |
| `paper` | CompactPaper | required | — | |
| `rules` | GoldRulesRef | required | — | |
| `files` | array of GoldPdfFile | required | — |≥ 1 items |
| `annotation` | CompactAnnotationActors | required | — | |
| `review` | CompactReview | required | — | |
| `revision` | CompactRevision \| null | optional | null | |

## Nested typed fields

#### `CompactContribution`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `contribution_id` | string | required | — |pattern `^\d{4}\.\d{4,5}:cand\d{2,}$` |
| `identifiers` | array of CompactIdentifier | required | — |≥ 1 items |
| `contribution_type` | CompactClaim | required | — | |
| `contribution_summary` | CompactClaim | required | — | |
| `paper_boundness` | CompactBoundness | required | — | |
| `quantities` | array of CompactQuantityGroup | optional | [] | |
| `quantity_conflicts` | array of CompactConflict | optional | [] | |

#### `CompactIdentifier`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `value` | string | required | — |min length 1 |
| `evidence` | array of GoldEvidence | optional | [] | |

#### `CompactClaim`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `value` | string | required | — |min length 1 |
| `evidence` | array of GoldEvidence | optional | [] | |

#### `CompactBoundness`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `value` | null \| string | required | — | |
| `evidence` | array of GoldEvidence | optional | [] | |

#### `CompactQuantityGroup`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `quantity` | string | required | — | |
| `values` | array of CompactValue | required | — |≥ 1 items |

#### `CompactValue`

The v1 numeric contract, with value first and its PDF evidence last.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `value` | null \| string | required | — | |
| `unit` | null \| string | optional | null | |
| `error` | null \| string | optional | null | |
| `lower_error` | null \| string | optional | null | |
| `upper_error` | null \| string | optional | null | |
| `limit_kind` | lower_limit \| none \| range \| upper_limit | optional | "none" | |
| `range_lower` | null \| string | optional | null | |
| `range_upper` | null \| string | optional | null | |
| `coordinate_format` | decimal_degrees \| sexagesimal_colon \| sexagesimal_dms \| sexagesimal_hms \| null | optional | null | |
| `condition` | null \| string | optional | null | |
| `interval_kind` | confidence \| credible \| ordinary_range \| prior_support \| statistical_interval \| unspecified \| null | optional | null | |
| `coverage` | GoldProbability \| null | optional | null | |
| `lower_quantile` | GoldProbability \| null | optional | null | |
| `upper_quantile` | GoldProbability \| null | optional | null | |
| `error_components` | array of CompactErrorComponent | optional | [] | |
| `representation` | distance_modulus \| linear \| log10 \| null | optional | null | |
| `source` | prior_work \| this_paper \| unclear | required | — | |
| `source_note` | null \| string | optional | null | |
| `context_evidence` | array of GoldEvidence | optional | [] | |
| `evidence` | array of GoldEvidence | optional | [] | |

#### `CompactErrorComponent`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `name` | string | required | — |min length 1 |
| `error` | null \| string | optional | null | |
| `lower_error` | null \| string | optional | null | |
| `upper_error` | null \| string | optional | null | |
| `evidence` | array of GoldEvidence | required | — |≥ 1 items |

#### `CompactConflict`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `conflict_id` | string | required | — |min length 1 |
| `quantity` | string | required | — |min length 1 |
| `reason` | CompactClaim | required | — | |
| `alternatives` | array of CompactValue | optional | [] | |

#### `CompactExclusion`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `exclusion_id` | string | required | — |min length 1 |
| `reason` | CompactClaim | required | — | |

#### `CompactCoverage`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `value` | complete \| partial | required | — | |
| `limitations` | array of CompactScopeLimitation | optional | [] | |

#### `CompactScopeLimitation`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `subject` | string | required | — |min length 1 |
| `value` | string | required | — |min length 1 |
| `evidence` | array of GoldEvidence | optional | [] | |

#### `CompactUnresolved`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `subject` | string | required | — |min length 1 |
| `value` | string | required | — |min length 1 |
| `evidence` | array of GoldEvidence | optional | [] | |

#### `CompactPaper`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `arxiv_id` | string | required | — |pattern `^\d{4}\.\d{4,5}$` |
| `title` | string | required | — |min length 1 |
| `version` | string | required | — |min length 1 |

#### `CompactAnnotationActors`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `original_annotator` | string | required | — |min length 1 |
| `original_annotated_at` | null \| string | optional | null | |
| `original_models` | array of string \| null | optional | null | |
| `reviewer` | string | required | — |min length 1 |
| `review_models` | array of string \| null | optional | null | |

#### `CompactReview`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `material` | CompactReviewConfirmation | required | — | |
| `scope` | CompactReviewConfirmation | required | — | |
| `answer_sha256` | string | required | — |pattern `^[0-9a-f]{64}$` |

#### `CompactReviewConfirmation`

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `value` | checked \| complete \| partial | required | — | |
| `basis` | string | required | — |min length 1 |
| `record_sha256` | string | required | — |pattern `^[0-9a-f]{64}$` |
| `reviewed_at` | null \| string | optional | null | |
| `differences` | array of string | optional | [] | |

#### `CompactRevision`

Latest local correction; earlier revisions are retained by Git.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `revised_by` | string | required | — |min length 1 |
| `decided_by` | string | required | — |min length 1 |
| `revised_at` | string | required | — |min length 1 |
| `basis` | string | required | — |min length 1 |
| `previous_answer_sha256` | string | required | — |pattern `^[0-9a-f]{64}$` |

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

#### `GoldProbability`

Explicitly printed coverage probability or quantile; consumers perform conversions
without writing them back.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `value` | string | required | — |min length 1 |
| `unit` | % \| fraction | required | — | |
| `evidence` | array of GoldEvidence | required | — |≥ 1 items |

#### `GoldRulesRef`

Scientific rules used for the original annotation; current scoring checks compatibility.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `version` | string | required | — |min length 1 |
| `sha256` | string | required | — |pattern `^[0-9a-f]{64}$` |

#### `GoldPdfFile`

One fixed PDF material; an unverified hash stays pending rather than fabricated.

| Field | Type | Presence | Default | Constraints |
|---|---|---|---|---|
| `file_id` | string | required | — |min length 1 |
| `name` | string | required | — |min length 1 |
| `sha256` | null \| string | optional | null | |
