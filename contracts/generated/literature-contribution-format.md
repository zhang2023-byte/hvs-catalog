<!-- Generated from hvs_catalog.records.literature_contribution. -->
# Literature contribution format

`literature_contribution.json` contains one paper's scientific contribution.
Its root contains only `metadata`, `stars`, and `notes`. It has no record
envelope or runtime information. The program assembles this file from stage snapshots.
The separate `run.json` file records execution status and archive references.

Schema: [literature_contribution.json](schemas/literature_contribution.json).
Complete fictional example: [literature_contribution.json](../../tests/fixtures/literature_contribution.json).

## Notes

The final `notes` field is an array of nonempty strings.
Put the roster note first, then quantity notes in saved roster order.
Prefix each quantity note with `identifiers[0].value` for its star and `: `.
Omit empty notes. Use `[]` when no note is necessary.
Each extraction stage still submits its own `notes` as a string.
The final assembly step must add the prefixes and preserve this order.

## Numerical forms and evidence

- Point value: `value`, optionally `error` or `lower_error` / `upper_error`.
  Symmetric and asymmetric errors cannot coexist. A reported one-sided error
  may stand alone; the other side is not filled in.
- Upper or lower limit: only `upper_bound` or `lower_bound`.
- Pure interval: both `lower_bound` and `upper_bound`, with no point value
  or error fields. Do not calculate a midpoint or infer an error.
- Optional `lower_inclusive` / `upper_inclusive` requires the matching bound.
  Use a boolean only when endpoint inclusion is reported. Otherwise omit it.
- Numerical components remain original strings, preserving signs, precision,
  trailing zeros, coordinate notation, and unevaluated expressions.
- Omit unreported optional fields; do not fill them with `null` or defaults.
  `unit` and `condition` are optional. Put reference frames and relevant
  statistical meanings or confidence levels in `condition`.
- Each identifier, contribution type, and value has a nonempty `evidence`
  array. Text evidence contains `location` and a faithful `quote`.
  Image evidence contains only `location`. Captions use text locations.

Exact quote mismatches are nonblocking audit observations for new executions.
The program preserves the submitted quote and records its location, result path, and matching reason.
Missing locations, unsupplied images, invalid structure, and damaged bindings remain errors.
An exact match does not authenticate scientific support.

## Validation and JSON access

Unknown fields and coercion of numbers to strings or integers to booleans
are rejected. Quantity identifiers reuse the existing science definitions
without changing their meaning. Structural validation does not authenticate
quotes or locations, decide scientific inclusion, resolve conflicts,
compare interval endpoints, or verify any physical value.

The public Python API is available from `hvs_catalog.records`:

```python
from hvs_catalog.records import (
    LiteratureContribution,
    literature_contribution_to_json,
    load_literature_contribution,
    parse_literature_contribution,
    save_literature_contribution,
)

document = load_literature_contribution("literature_contribution.json")
text = literature_contribution_to_json(document)
validated = parse_literature_contribution(text)
save_literature_contribution("output/literature_contribution.json", validated)
schema = LiteratureContribution.model_json_schema()
```

Parsing accepts a JSON string or a dictionary and raises a Pydantic
`ValidationError` for invalid structure or JSON. Saving validates before
writing UTF-8 JSON, creates parent directories, and keeps `notes` last.
File I/O errors propagate to the caller. This API does not add a transaction
or connect to workspace persistence.

## Fields

#### LiteratureContribution

The complete literature_contribution.json scientific document.

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `metadata` | `LiteratureMetadata` | yes |  |  |
| `stars` | list of `LiteratureStar` | yes |  | Contributions to individual stars; may be empty. |
| `notes` | list of `string` | yes |  | Brief notes about relevant exclusions, suspected errors, ambiguities, and handling decisions with their basis. Put the roster note first. Follow it with quantity notes in saved roster order. Prefix each quantity note with that star's first identifier and ': '. Omit empty notes. Use an empty array when no note is necessary. Notes do not replace field evidence. |

#### LiteratureMetadata

Bibliographic identity of the fixed paper version.

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `arxiv_id` | `string` | yes | pattern `^(?:[0-9]{4}\.[0-9]{4,5}\|[a-zA-Z-]+(?:\.[A-Z]{2})?/[0-9]{7})$` | Bare arXiv identifier, without a version suffix. |
| `version` | `string` | yes | pattern `^v[1-9][0-9]*$` | arXiv paper version, such as v1; not a format version. |
| `title` | `string` | yes | minLength 1 | Paper title. |
| `authors` | list of `string` | yes |  | Paper authors in reported order. |

#### LiteratureStar

This paper's contribution to one star; cross-paper association is separate.

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `identifiers` | list of `LiteratureIdentifier` | yes | minItems 1 | Identifiers with individual evidence. |
| `contribution_type` | `LiteratureContributionType` | yes |  |  |
| `quantities` | list of `LiteratureQuantity` | yes |  | Retained quantities; may be empty if no values are retained. |

#### LiteratureIdentifier

One reported identifier and its supporting evidence.

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `value` | `string` | yes | minLength 1 | Star identifier. |
| `evidence` | list of `TextEvidence` \| `ImageEvidence` | yes | minItems 1 | Evidence for this identifier. |

#### LiteratureContributionType

The paper's contribution to this star.

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `value` | `candidates_found` / `follow_up` | yes |  | Contribution category. Scientific inclusion rules are separate. |
| `evidence` | list of `TextEvidence` \| `ImageEvidence` | yes | minItems 1 | Evidence for this contribution type. |

#### LiteratureQuantity

Reported values grouped by one existing physical quantity identifier.

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `quantity` | `ra` / `dec` / `distance` / `parallax` / `proper_motion_ra` / `proper_motion_dec` / `radial_velocity` / `galactocentric_radius` / `galactic_rest_frame_velocity` / `bound_probability` / `unbound_probability` | yes |  | Short quantity identifier from the existing science definitions. |
| `values` | list of `LiteratureValue` | yes | minItems 1 | Reported values, each with its own evidence. |

#### LiteratureValue

One point value, one-sided limit, or pure interval with local evidence.

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

#### TextEvidence

An evidence excerpt attributed to one supplied text block.

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `location` | `string` | yes | pattern `^T:p[1-9][0-9]*-b[0-9]+$` | Complete text location, such as T:p3-b5. |
| `quote` | `string` | yes | minLength 1 | Faithful excerpt from this text block. |

#### ImageEvidence

A supplied image location without a text transcription.

| Field | Type | Required | Constraints | Notes |
|---|---|---|---|---|
| `location` | `string` | yes | pattern `^I:p[1-9][0-9]*-b[0-9]+$` | Complete image location, such as I:p5-b6. |
