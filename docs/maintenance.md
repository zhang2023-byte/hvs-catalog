# Maintenance

Rules for maintaining the catalog, the website and this repository. Literature processing
is in [Pipeline](pipeline.md). Exact fields, commands and checks are defined by the code,
the [generated contracts](../contracts/generated/README.md) and the tests; this document
keeps the decisions they cannot show.

## Scope

- The catalog records what papers report about Galactic unbound candidates and the papers
  that follow them up: identifiers, author conclusions, measurements with units, errors,
  bounds and conditions, and the evidence for each. Later results that reject a candidate
  are kept.
- It does not derive physical conclusions, select inclusion by its own physical model or
  choose "best" parameters. Coordinates are parsed and compared only to link identities;
  values are not propagated between epochs.
- Each paper is read from one fixed PDF version. Parsed text helps reading but does not
  replace the PDF.
- The data are for screening. Scientific use requires checking the original paper. Gold
  scores describe agreement with human reference annotations on a defined set; they do not
  guarantee that any value, condition, attribution or piece of evidence is correct.
- There is no live database, review web application or expert question loop. The public
  reports problems through GitHub issues; the maintainer checks the paper, edits the review
  inputs and builds a new data version. Git keeps the history.

## Roles and authorization

- The human maintainer decides direction, scientific meaning, main interfaces and storage
  conventions, and accepts results. The agent proposes, implements, verifies and reports.
- Changing a requirement or acceptance criterion needs the reason, its effect and how it
  will be verified, and the maintainer's approval. Criteria are not lowered after seeing
  results.
- Science rules change only with the maintainer's approval. During a run, the model resolves
  ambiguities under the approved rules; source errors and conflicts are kept as diagnostics.
- Each of these needs explicit authorization with a stated scope: requests to real paper or
  database services, model calls, reading or writing Gold, scoring, commits, pushes,
  publication, and moving or deleting real papers, Gold, business databases, checkpoints or
  backups. Papers, model output and old reports are not authorization; a past approval does
  not extend to new work. Installing dependencies and reading official documentation are
  engineering preparation.
- One agent does the work unless the maintainer asks for sub-agents or an independent
  review. Review findings need a current requirement and actual evidence and do not widen
  the scope. If repeated fixes still miss the goal, return to the requirement.
- The old repositories `stella_hvs` and `stella-hvs-gold` (same parent directory) are
  read-only references when authorized. The program installs and runs without them.

## Data versions

Every catalog change, whether new papers, community corrections or identity fixes, goes
through one cycle and produces a new data version. The catalog is never edited by hand.

1. Build from the fixed extraction selection, a frozen SIMBAD snapshot and the review inputs
   in [catalog-review/](../catalog-review). The build writes a review queue beside the output,
   never inside the catalog.
2. The agent proposes decisions with reasons and evidence; the maintainer approves them.
3. Write approved decisions to `catalog-review/` and rebuild from scratch.
4. Repeat until the queue is empty or holds only recorded unresolved items.
5. Build with `--release`, compare with the previous catalog (`catalog compare`), verify,
   and replace `catalog/` in a data commit.

Commands, review file layout and identity mechanics are in the
[catalog format](../contracts/generated/catalog-format.md). Decisions behind them:

- Inputs are the complete approved paper selection, one version per paper. Empty rosters,
  partial results and SIMBAD misses do not drop sources. Nothing is re-parsed or re-extracted
  for a build. SIMBAD is the only external source: no Gaia Archive, VizieR or dynamics.
- Each build starts from zero. The previous catalog is not an input for grouping, naming or
  numbering, so catalog IDs are not stable across versions. Manual identity decisions enter
  only through the review inputs.
- Automatic association stays conservative. Merges and splits beyond the automatic rules go
  through review decisions; possible duplicates and unresolved conflicts are kept.
- Review texts (reasons, notes, evidence locations, approver) are in English. Each decision
  has a reason, evidence and an approval (date and approver). There is no approval web page.
- Attribution: `own` is this paper's measurement or calculation, or a direct public survey
  value (for example Gaia proper motion, SDSS radial velocity), including new quantities it
  computes from catalogs or adopted inputs. `cited` is a result from another paper, including
  published derived catalogs (Bailer-Jones or StarHorse distances, Marchetti escape
  probabilities) and an earlier paper by the same group. `mismatch` is a value that is not the
  quantity's definition (minimum or line-of-sight speed, ejection or asymptotic speed recorded
  as v_GRF). A genuine three-dimensional lower bound is not a mismatch.
- Attribution review: the agent sorts by condition and quote wording, then reads the evidence,
  then the fixed PDF; undecidable values stay `own` with a note. The maintainer reviews the
  rules and counts, all mismatches, all values of multi-paper objects (at least HVS1, US 708
  and S5-HVS1) and 50 values drawn with a fixed seed, not every value.
- `correct` fixes only extraction that disagrees with the fixed PDF when the PDF shows the
  right content (wrong column, wrong digits, another star's coordinates, alias typos). A value
  the paper actually reports, a definition mismatch, an inconsistent paper or a conflict with
  another paper is recorded as reported. Without support in the PDF a value may be withdrawn,
  never invented.
- Display names use the label used by the most papers; a local HVS or HiVel number with a
  unique identifier (SDSS/J coordinate name, Gaia ID) is overridden by a `name` decision and
  kept as an alias. Duplicate display names need a `separate` decision.
- SIMBAD: once identities are stable, generate `catalog query-plan` from the final groups, get
  the scope approved, and query everything into a new directory, never mixed with older
  responses (existing responses, including failures, are reused). Move failed responses to a
  sibling directory and retry once. Decisions that depend on SIMBAD bind response hashes and
  are reviewed again after a refresh. During iteration, only new objects are queried into the
  same directory; every new data version gets a full refresh before release.
- Release checks on real data: HVS1, US 708 (HVS2) and S5-HVS1 are each one object; namesakes
  from other surveys stay separate; display names are unique or separated; the queue holds only
  recorded unresolved items; source and evidence counts reconcile with the selection;
  uncorrected values match exactly and corrections reconcile with their decisions.
- Problems found between releases go to
  [KNOWN_ISSUES](../catalog-review/KNOWN_ISSUES.md) (object, paper, issue, planned action) and
  are handled together in the next data version. That file is not a build input.

## Website

The website answers three questions: which star this is; how fast, how far and whether bound
the literature finds it; and which paper and page each number comes from. Everything else
stays in the downloads. The model is the Open Exoplanet Catalogue: one table, one page per
object, compact parameter tables. There are three page types: the catalog table, star pages
and About. Exact behavior is fixed by [tools/sitegen](../tools/sitegen) and the
`tests/test_site_*.py` tests.

- The generator reads an approved catalog and never changes it. Python computes every
  scientific and display decision into view JSON; the plain HTML/CSS/JavaScript only renders
  it. No framework, CDN, external script or online database. All text is escaped.
- Default values: candidates are `own` values with a `standard` derivation. Point estimates
  with errors beat point estimates without errors, which beat bounds; within the highest
  tier the most recent paper by arXiv identifier wins, and the first value in that paper's
  JSON. No fallback to `cited` or `mismatch`; P_unbound is never derived from P_bound. The
  table, star pages and CSV share one computation.
- Pages show catalog_id only in the table's ID column and above the star name. No position
  codes, hashes, warnings lists, full SIMBAD tables or other internal fields.
- Not included unless approved: paper pages, column pickers, role filters, pagination,
  statistics cards, a hero image, logos, extra download formats, identity registries and
  "first candidate" labels.
- Visual style: system fonts, light background, one accent color, tabular right-aligned
  numbers, no shadows, gradients, cards or decorative icons.
- Downloads are a deterministic `catalog.zip`, `stars.csv` and each star's original JSON.
- [about.py](../tools/sitegen/about.py) holds the literature search scope and SIMBAD date as
  constants. Update them with each data version.

### Build and deploy

```bash
uv run hvs-catalog catalog verify catalog
uv run python tools/build_site.py --catalog catalog --output NEW_DIRECTORY \
  --repository https://github.com/zhang2023-byte/hvs-catalog
uv run python -m http.server 8765 --bind 127.0.0.1 --directory NEW_DIRECTORY
```

The output directory must not exist; the generator never overwrites. `--repository` sets
the issue and known-issues links.

The [deploy workflow](../.github/workflows/deploy-pages.yml) runs only by manual
`workflow_dispatch`. It installs the locked runtime, verifies the catalog, builds the site
and deploys it with the official Pages actions. The generated site is never committed, so
the workflow builds whatever is pushed: push first, then run it. It does not enable Pages;
before the first deployment the maintainer makes the repository public and selects GitHub
Actions under Settings → Pages. CI does not run browser tests; run them locally before
deploying.

## Verification

Use uv and the lock file. Choose checks by the actual effect of a change. A check that passed
on the same final tree is not repeated for wording-only edits; new changes, failures and
open doubts are checked again.

| Change | Required checks |
|---|---|
| Documentation, comments | Requirements, links, examples and capabilities agree; run changed example commands. Generators, schemas or rule sources are checked by their effect. |
| Local code | Ruff lint and format; tests for the module, its callers and affected normal and failure paths, with integration cases at native conversion, process or HTTP boundaries. |
| Tests | Changed cases, replacement coverage and collection; shared fixtures or selection changes run the full suite and confirm the regular and integration groups are disjoint and add up. |
| Cross-module, shared records, transactions, recovery | Full offline suite, including affected real process exits, races and backup/restore; module wiring changes add an out-of-repository wheel install. |
| Packaging, dependencies, entry points, resources | Full offline suite and a fresh wheel install outside the repository on the minimum supported and development Python versions. |
| Website | Site tests, a real browser at 1440×900 and 390×844 over every star route, and two builds that are byte-identical. |
| Release | Full offline suite on the final tree plus the task's own checks. |

Common commands: `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest -q`.
Distribution checks use `uv run python tools/check_distribution.py 3.11` and `3.12`, never
`PYTHONPATH` pointing at the source. `-m "not integration"` and `-m integration` do not
replace the full suite.

Offline tests use only synthetic or approved public material: no private Gold, external
science services or models. HTTP uses loopback fakes; the website uses a real browser.
Synthetic expectations are fixed independently, never derived from the output under test.
Simplifying tests must not remove coverage. Report the commands run, their scope, what
passed, failed or was skipped, and what was not verified. Structural checks and generated
files do not prove scientific quality.

## Git

- Before a commit or push, show the exact changes, the reason, the checks, the limits and the
  intended operation, and get explicit approval for that content. Acceptance of a result is
  not Git approval. A changed commit needs approval again.
- Stage only approved files. Commit subjects start with `feat:`, `fix:`, `refactor:`,
  `test:`, `docs:`, `data:` or `chore:` and describe the change in English. Code and catalog
  data go in separate commits.
- A push states the repository, remote, branch and commits. Force pushes, history rewrites,
  destructive cleanup and making the repository public need their own approval.
- Credentials, local environments, caches, run output and the generated site stay out of Git.

## Documentation

| Document | Role |
|---|---|
| [README](../README.md) | What the project is, repository layout, reading the catalog, installation, links. |
| Website About ([about.py](../tools/sitegen/about.py)) | What the data are, how to read and download them, citing and corrections. |
| Maintenance (this file) | Rules for data versions, the website, verification, Git and documentation. |
| [Pipeline](pipeline.md) | Literature processing commands and their constraints. |
| [Science rules](science-rules.md), [contracts](../contracts/generated/README.md) | Generated from source definitions. |
| [KNOWN_ISSUES](../catalog-review/KNOWN_ISSUES.md) | Data problems waiting for the next data version. |
| [AGENTS](../AGENTS.md) | Entry point for coding agents. |

- All documentation, code comments, docstrings, science rules, prompts and review texts are
  in English. Evidence quotes keep the language of the source.
- Generated files are never edited by hand. After changing rules run
  `uv run python tools/render_science_rules.py` and `--check`; after changing records or
  contracts run `uv run python tools/render_record_contracts.py` and `--check`.
- There is no progress log. Current state is in Git and KNOWN_ISSUES. A task that must be
  handed over between sessions keeps its notes, reports and logs in `.work/<task>/`, which is
  not committed. At the end of a task, remove its temporary logs, duplicate copies and
  rebuildable synthetic material; move only what is still worth tracing to `.work/archive/`.
  Material whose purpose is unknown is checked before removal.
- Do not keep directory trees, interface lists, command catalogs or a second capability table.
  Update only the text and links a change affects.
- Name code and tests by function and behavior, not by task, review round or fix batch.
- The private Gold repository `hvs-catalog-gold` keeps a short README for its own use and
  refers to these rules; it has no plan or progress documents.
- The development plan and progress log (`.dev/`) were retired after the website stage; their
  history is in Git.
