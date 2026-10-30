# Literature pipeline

How papers become extraction results, and the constraints on each stage. Catalog builds
from these results are in [Maintenance](maintenance.md#data-versions). Real services, model
calls and Gold access need the authorization described there. Exact options are in each
command's `--help`.

## Flow

Acquisition → PDF parsing → roster → quantities → catalog build. The maintainer chooses the
papers, versions and scope; the agent runs the stages through the shared CLI and reports.
No stage starts the next one.

### Prepare papers

Dates, search terms, paths and scope are chosen by the operator. A business space is named by
`--space`. Search, download, MinerU parsing and model extraction contact external services;
initialization, configuration checks and status are offline.

```bash
hvs-catalog prepare init --space /path/to/literature --batch sample --from 2026-01-01 --to 2026-01-31 --term "hypervelocity star"
hvs-catalog prepare discover --space /path/to/literature --batch sample
hvs-catalog prepare download --space /path/to/literature --batch sample
hvs-catalog prepare parse --space /path/to/literature --batch sample --env-file /path/to/.env
hvs-catalog prepare status --space /path/to/literature --batch sample
```

Parsing uses the official MinerU v4 `vlm` API with `MINERU_TOKEN`; extraction uses Token Dance
with `TOKENDANCE_API_KEY`. Credentials come from the process environment or an explicit
private environment file and never enter Git. A space keeps the fixed PDF, the original
parser output and its index.

### Extract

Write the extraction configuration and check both stages' model, provider, input mode,
concurrency and timeouts:

```bash
hvs-catalog extract config > /path/to/extraction.json
hvs-catalog extract check --config /path/to/extraction.json
```

Take `preparation.execution_id` from `reports/preparation-manifest.json`; `--request-key`
names this fixed input:

```bash
hvs-catalog extract run --space /path/to/literature --preparation PREPARATION_ID --request-key sample-paper --config /path/to/extraction.json --env-file /path/to/.env
hvs-catalog extract status --space /path/to/literature --execution EXECUTION_ID
```

`extract run` performs roster, quantities and assembly and returns the execution ID and
result references. The results are `run.json`, `literature_contribution.json` when present,
and `evidence_audit.json` of the same generation; read them by the returned references, not
by guessing the latest directory. `complete`, `partial` and `failed` describe delivery, not
scientific correctness. `extract resume` continues an unfinished execution; an execution ended
with `extract close --end` sends no further requests. `extract batch` takes several
`--preparation` options, or `--manifest` for fixed executions across spaces (`--check-only`
verifies only). Naming an execution by arXiv ID must be bound at creation through
`start_extraction(..., execution_id=arxiv_id)` in [entry.py](../src/hvs_catalog/pipeline/entry.py).

### Current literature root

The maintainer's local `.work/literature/` (not in Git) holds the 413 papers of the current
data version: an arXiv `all:` search for "hypervelocity star", "hyper-velocity star", "high
velocity star" and "unbound star", first submitted 2005-01-01 to 2026-10-23 (UTC, inclusive),
without category limits. This search is not claimed to be exhaustive.

- `runs/<bare arXiv ID>/results/` holds only the final `literature_contribution.json`,
  `evidence_audit.json` and `run.json` (`hvs.final-literature-result`). Old-style IDs replace
  `/` with `_` in directory names only.
- `history/extraction/` keeps the original executions, repairs and indexes unchanged. They are
  verified read-only; old orchestration scripts are not run and the root is not resumed.
- Catalog builds read these results directly; see the
  [catalog format](../contracts/generated/catalog-format.md#final-literature-inputs).

## Sources and materials

- Search queries each term over monthly windows without category filters, checks the UTC
  first-submission date, merges by bare arXiv ID and keeps every matching term. Downloading
  pins the version that is latest at download time; a retry never switches version. Each space
  holds one fixed PDF version per paper and refuses different content; a new version needs a
  new space. No scheduled searches, automatic new-version handling or journal crawling.
- arXiv requests are serialized per local user across processes, at least three seconds apart
  on one connection. Server cooldowns are respected; temporary errors retry a limited number of
  times; refusal stops. Network waits are never inside a database transaction.
- PDF admission checks type, size, page structure, encryption and resource limits. Optional
  LaTeX is archived only. A journal PDF and a preprint are linked only with evidence, never by
  title alone. Imports state identity, version and origin.
- Parsing uploads the original bytes to the MinerU v4 `vlm` batch API with English, formula and
  table recognition on and OCR off, and uses the default Markdown/JSON/image output. Its content
  is only mapped to page, block and image indices; nothing is filled in or rewritten. No local
  inference, cropping, table stitching or runtime patches. Unknown batch creation is never
  resubmitted; resuming only queries known tasks. Downloads keep HTTPS verification.
- Model material keeps the official text, tables, author captions and original images and drops
  parser-generated image descriptions. Steps select images up to their route's limit (GLM 50 per
  request; other routes a local guard of 600): tables first in paper order, then other images.
  Text-only mode sends no images and cannot cite them.
- Space layout: `papers/<arXiv ID>/` (`paper.pdf`, `metadata.json`), `mineru/`, `runs/`,
  `reports/` and `.state/` (history, database, recovery material). Parsing success and
  `materials_complete` do not prove the text is faithful to the PDF.

## Science rules and model input

- [rules.toml](../src/hvs_catalog/resources/science/rules.toml) is the only source of the
  model's science rules: four approved rules and the definitions and common units of 11
  quantities. Its reading copy is [science-rules.md](science-rules.md). Changes to meaning need
  the maintainer's approval.
- System prompts contain only the task, the science rules, the material description and the
  submission behavior; field filling lives in the tool descriptions and schemas. Prompts never
  contain Gold, scores, expert decisions or run management. The model writes scientific content
  and evidence; the program supplies IDs, revisions, references and run data.
- Models, routes and parameters come from a restricted configuration, separate from
  credentials. Actual sent and omitted parameters are recorded; unknown server defaults stay
  unknown. Changing one step's model does not affect other steps. Models, routes and modes never
  switch automatically.
- Default configuration: DeepSeek V4.1 Flash, roster Max and quantities Low, output limit
  393216, concurrency 100, at most two format corrections per submission and three network
  attempts per request. To pin the 1017 model, set both stages to `deepseek-v4.1-flash-1017`
  with provider `deepseek`. Frozen historical configurations do not follow default changes.
- All local requests share one rate limit. Only isolated network failures retry (connection
  failures before sending, 429, temporary 5xx/408/499, broken streams and timeouts); errors in
  authentication, format, parameters or size, and unknown completions without a response
  archive, never retry.

## Extraction and evidence

- The roster model expands identifier ranges and submits each star; the program never guesses
  members. Quantities are submitted per star as `quantities → quantity + values`.
- Original number strings, error and bound forms, units, conditions, sources and evidence are
  kept as written. Nothing is evaluated, converted, zero-padded or completed. One-sided errors,
  zeros, negatives and multiple values are valid. Endpoint inclusion is filled only when stated.
- Each roster task and each star's quantities get an initial submission plus at most two full
  corrections with full revalidation. When corrections run out, the latest readable round is
  used; rounds are never spliced or chosen by valid count. Invalid stars or values are dropped
  whole with their reasons; other valid results are kept. A legal empty roster is a complete
  delivery.
- Ambiguity is resolved by the model from the paper's evidence with a short note. Missing
  material, parse failure, source errors or conflicts, not reported and no overall conclusion
  are kept apart. An author error needs support from the text, a definition or a clear logical
  contradiction. Unexplained conflicts for the same object and condition become diagnostics
  with both values, not measurements.
- Text evidence is checked for file, page, block and quote; image evidence only for location
  and binding, not content. A non-verbatim quote is a warning that does not block results.
  Invalid JSON or schema, missing locations, unsent images and broken bindings are hard errors.
  `evidence_audit.json` records the quote mismatches of the final result beside `run.json`.
- Results follow [literature_contribution.json](../contracts/generated/schemas/literature_contribution.json)
  (`metadata/stars/notes`) and its [format](../contracts/generated/literature-contribution-format.md).

## Runs and storage

- Scientific records are immutable revisions with exact references. Unknown formats, missing
  fields, escaping paths and wrong paper or object bindings are refused.
- Fixed inputs, prompts and images are shared by hash; each request stores its exact input
  references and parameters. Pending records are saved before a call; unknown completions stay
  `completion_unknown`. Failed responses keep the redacted HTTP body or SSE within size limits.
  Attempts, calls, known tokens, retries, failures and timestamps are recorded; unknown values
  are `null`. No currency costs are computed.
- All writes, executions and backups in a space share an exclusive lock; a busy space returns
  busy. Different papers' rosters and different stars' quantities share one scheduler; workers
  make network calls and one coordinator archives, validates and writes.
- Backups are taken under the lock and exclude credentials; restore goes only to a new
  directory and is verified in a new process. Ended executions are compacted after
  verification; failed or interrupted ones stay recoverable.
- Historical paused executions remain readable only; they are not resumed or completed under
  new rules. Question, decision and release records are no longer supported record types.

## Benchmarks, Gold and scoring

- A benchmark root is one storage and move unit. Each round selects `mineru_local` or
  `mineru_cloud` and text-only or image mode explicitly, with no fallback. `import-materials`,
  `preview` and `prepare` call no model; `run --round execNN` executes or resumes; repeats get a
  new number and ended rounds cannot resume. Derived network reruns (`repair-*`) stay within an
  authorized target and budget and never rewrite the original result.
- Gold lives in the private repository `hvs-catalog-gold`: per paper `data/<arxiv_id>/` with
  `paper.pdf` (ignored by Git) and a human-confirmed `annotation.json`. Changes go through a
  working copy, validation and a full diff, the maintainer's confirmation, an atomic save and an
  approved commit. See the [Gold contracts](../contracts/generated/gold/README.md).
- Scoring is programmatic and reports L0 (delivery), L1 (objects) and L2 (values) without a
  combined score; see the [scoring contract](../contracts/generated/scoring/README.md) and
  [fixtures](../tests/fixtures/scoring). Only strict matches and approved format equivalences
  count. A new equivalence needs approval, a new scoring version and rescoring of all affected
  inputs; Gold, predictions and old reports are never rewritten.
- Experiments fix materials, code, rules, prompts, model parameters, rounds, concurrency,
  retries, metrics, budget and stop conditions in advance and get approval. The overall API
  budget reference is 1000 RMB; warn and wait before a run that may exceed 100 RMB. Failures,
  empty and partial results stay in the denominator.
- Gold50 was used in development; results on it are not a blind test. An independent
  evaluation freezes the tested versions first and uses papers not used in development.
- Open: the 2026-10-18 proposal to stop deriving approximation markers from conditions in
  scoring is not implemented.
